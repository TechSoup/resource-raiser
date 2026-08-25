#!/usr/bin/env python3
"""Canonical entity resolver — the cross-source spine.

Resolves an NL entity mention (with a type hint) to ONE canonical identity
(a Wikidata QID) and pulls every source key it carries (CIK, EIN, FIPS, GNIS,
ticker, LEI). Each source then uses its own key from the crosswalk, so the same
real-world entity lines up across sources for joins. Disambiguation (city vs
university, company vs band) is delegated to a `pick` callback (the LLM).

Results are cached to resolver_cache.json — resolutions are stable.
"""
import asyncio, os, json, urllib.request, urllib.parse

import httpx

import runtime

WD = "https://www.wikidata.org/w/api.php"
CACHE = os.path.join(os.path.dirname(__file__), "resolver_cache.json")

# Wikidata property -> our key name (one crosswalk, all sources)
PROPS = {
    "P5531": "cik", "P249": "ticker", "P1278": "lei", "P1297": "ein",
    "P774": "fips_place", "P882": "fips_county", "P5087": "fips_state", "P590": "gnis",
}

if os.path.exists(CACHE):
    with open(CACHE) as f:
        _cache = json.load(f)
else:
    _cache = {}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ard-data-demo/1.0 (guha@guha.com)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


async def _get_async(url, *, context):
    context.check()
    if context.http_client is None:
        raise RuntimeError("async Wikidata access requires QueryContext.http_client")
    try:
        response = await context.provider_call("wikidata", lambda: context.http_client.get(
            url, headers={"User-Agent": "ard-data-demo/1.0 (guha@guha.com)"},
            timeout=min(20, context.remaining() or 20)))
    except (asyncio.CancelledError, runtime.QueryCancelled):
        raise
    except httpx.RequestError as exc:
        raise RuntimeError(f"Wikidata request failed: {exc}") from exc
    response.raise_for_status()
    return response.json()


def _search(mention, limit=7):
    q = urllib.parse.quote(mention)
    return _get(f"{WD}?action=wbsearchentities&search={q}&language=en&type=item&format=json&limit={limit}").get("search", [])


async def search_async(mention, limit=7, *, context):
    q = urllib.parse.quote(mention)
    data = await _get_async(
        f"{WD}?action=wbsearchentities&search={q}&language=en&type=item&format=json&limit={limit}",
        context=context)
    return data.get("search", [])


def _claim_values(entity):
    keys = {}
    for prop, name in PROPS.items():
        if prop in entity.get("claims", {}):
            try:
                value = entity["claims"][prop][0]["mainsnak"]["datavalue"]["value"]
                keys[name] = value.get("id", value) if isinstance(value, dict) else value
            except Exception:
                pass
    return (entity.get("labels", {}).get("en") or {}).get("value"), keys


def _claims(qid):
    e = _get(f"{WD}?action=wbgetentities&ids={qid}&props=claims|labels&format=json")["entities"][qid]
    return _claim_values(e)


async def claims_async(qid, *, context):
    data = await _get_async(
        f"{WD}?action=wbgetentities&ids={qid}&props=claims|labels&format=json", context=context)
    return _claim_values(data["entities"][qid])


async def instance_of_async(qid, *, context):
    ck = f"p31|{qid}"
    if ck in _cache:
        return _cache[ck]
    entity = (await _get_async(
        f"{WD}?action=wbgetentities&ids={qid}&props=claims&format=json", context=context))["entities"][qid]
    out = []
    for claim in entity.get("claims", {}).get("P31", []):
        try:
            out.append(claim["mainsnak"]["datavalue"]["value"]["id"])
        except Exception:
            pass
    _cache[ck] = out
    return out


async def class_labels_async(qids, *, context):
    qids = [qid for qid in dict.fromkeys(qids) if qid]
    out, need = {}, []
    for qid in qids:
        ck = f"clabel|{qid}"
        if ck in _cache:
            out[qid] = _cache[ck]
        else:
            need.append(qid)
    for offset in range(0, len(need), 40):
        chunk = need[offset:offset + 40]
        entities = (await _get_async(
            f"{WD}?action=wbgetentities&ids={'|'.join(chunk)}&props=labels&format=json",
            context=context))["entities"]
        for qid, entity in entities.items():
            out[qid] = _cache[f"clabel|{qid}"] = (
                entity.get("labels", {}).get("en") or {}).get("value", "")
    return out


async def hierarchy_async(qid, max_depth=4, *, context):
    ck = f"hier|{qid}"
    if ck in _cache:
        return _cache[ck]
    out, seen, current = [], set(), qid
    while current and current not in seen and len(out) < max_depth:
        seen.add(current)
        entity = (await _get_async(
            f"{WD}?action=wbgetentities&ids={current}&props=claims|labels&format=json",
            context=context))["entities"][current]
        label, keys = _claim_values(entity)
        out.append({"qid": current, "label": label, "keys": keys})
        try:
            current = entity["claims"]["P131"][0]["mainsnak"]["datavalue"]["value"]["id"]
        except Exception:
            current = None
    _cache[ck] = out
    return out


async def resolve_async(mention, type_hint, pick, *, context):
    ck = f"{type_hint}|{mention}".lower()
    if ck in _cache:
        return _cache[ck]
    candidates = await search_async(mention, context=context)
    if not candidates:
        return None
    chosen = pick(mention, type_hint, candidates)
    qid = await chosen if hasattr(chosen, "__await__") else chosen
    qid = qid or candidates[0]["id"]
    label, keys = await claims_async(qid, context=context)
    out = {"qid": qid, "label": label, "keys": keys}
    _cache[ck] = out
    return out


def instance_of(qid):
    """P31 class QIDs. Already present in the _claims response, so this costs no extra call
    when the claims are cached, and it is the only evidence that distinguishes a city from a
    university when neither carries a registry identifier."""
    ck = f"p31|{qid}"
    if ck in _cache:
        return _cache[ck]
    try:
        e = _get(f"{WD}?action=wbgetentities&ids={qid}&props=claims&format=json")["entities"][qid]
        out = []
        for c in e.get("claims", {}).get("P31", []):
            try:
                out.append(c["mainsnak"]["datavalue"]["value"]["id"])
            except Exception:
                pass
    except Exception:
        out = []
    _cache[ck] = out
    return out


def class_labels(qids):
    """English labels for class QIDs, one batched request."""
    qids = [q for q in dict.fromkeys(qids) if q]
    out, need = {}, []
    for q in qids:
        ck = f"clabel|{q}"
        if ck in _cache:
            out[q] = _cache[ck]
        else:
            need.append(q)
    for i in range(0, len(need), 40):
        chunk = need[i:i + 40]
        try:
            e = _get(f"{WD}?action=wbgetentities&ids={'|'.join(chunk)}&props=labels&format=json")["entities"]
        except Exception:
            continue
        for q, v in e.items():
            out[q] = _cache[f"clabel|{q}"] = (v.get("labels", {}).get("en") or {}).get("value", "")
    return out


def hierarchy(qid, max_depth=4):
    """Walk 'located in' (P131) from an entity up to its state, returning the containment
    chain [self, county, state] most-specific first — the ordered granularity alternatives
    a place query can BACKTRACK through (place -> containing county -> state)."""
    ck = f"hier|{qid}"
    if ck in _cache:
        return _cache[ck]
    out, seen, cur = [], set(), qid
    while cur and cur not in seen and len(out) < max_depth:
        seen.add(cur)
        e = _get(f"{WD}?action=wbgetentities&ids={cur}&props=claims|labels&format=json")["entities"][cur]
        keys = {}
        for p, name in PROPS.items():
            if p in e.get("claims", {}):
                try:
                    v = e["claims"][p][0]["mainsnak"]["datavalue"]["value"]
                    keys[name] = v.get("id", v) if isinstance(v, dict) else v
                except Exception:
                    pass
        out.append({"qid": cur, "label": (e.get("labels", {}).get("en") or {}).get("value"), "keys": keys})
        cur = None
        if "P131" in e.get("claims", {}):
            try:
                cur = e["claims"]["P131"][0]["mainsnak"]["datavalue"]["value"]["id"]
            except Exception:
                cur = None
    _cache[ck] = out
    return out


def resolve(mention, type_hint, pick):
    """pick(mention, type_hint, candidates) -> chosen QID (candidates have id/label/description)."""
    ck = f"{type_hint}|{mention}".lower()
    if ck in _cache:
        return _cache[ck]
    cands = _search(mention)
    if not cands:
        return None
    qid = pick(mention, type_hint, cands) or cands[0]["id"]
    label, keys = _claims(qid)
    out = {"qid": qid, "label": label, "keys": keys}
    _cache[ck] = out
    return out
