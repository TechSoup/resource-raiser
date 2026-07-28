#!/usr/bin/env python3
"""Canonical entity resolver — the cross-source spine.

Resolves an NL entity mention (with a type hint) to ONE canonical identity
(a Wikidata QID) and pulls every source key it carries (CIK, EIN, FIPS, GNIS,
ticker, LEI). Each source then uses its own key from the crosswalk, so the same
real-world entity lines up across sources for joins. Disambiguation (city vs
university, company vs band) is delegated to a `pick` callback (the LLM).

Results are cached to resolver_cache.json — resolutions are stable.
"""
import os, json, threading, urllib.request, urllib.parse
_LOCK = threading.Lock()

WD = "https://www.wikidata.org/w/api.php"
CACHE = os.path.join(os.path.dirname(__file__), "resolver_cache.json")

# Wikidata property -> our key name (one crosswalk, all sources)
PROPS = {
    "P5531": "cik", "P249": "ticker", "P1278": "lei", "P1297": "ein",
    "P774": "fips_place", "P882": "fips_county", "P5087": "fips_state", "P590": "gnis",
}

_cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ard-data-demo/1.0 (guha@guha.com)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _search(mention, limit=7):
    q = urllib.parse.quote(mention)
    return _get(f"{WD}?action=wbsearchentities&search={q}&language=en&type=item&format=json&limit={limit}").get("search", [])


def _claims(qid):
    e = _get(f"{WD}?action=wbgetentities&ids={qid}&props=claims|labels&format=json")["entities"][qid]
    keys = {}
    for p, name in PROPS.items():
        if p in e.get("claims", {}):
            try:
                v = e["claims"][p][0]["mainsnak"]["datavalue"]["value"]
                keys[name] = v.get("id", v) if isinstance(v, dict) else v
            except Exception:
                pass
    label = (e.get("labels", {}).get("en") or {}).get("value")
    return label, keys


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
    with _LOCK:
        _cache[ck] = out
        json.dump(_cache, open(CACHE, "w"))
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
    with _LOCK:
        _cache[ck] = out
        json.dump(_cache, open(CACHE, "w"))
    return out
