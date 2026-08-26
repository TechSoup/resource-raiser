#!/usr/bin/env python3
"""Thin ARD client — calls a remote Agent Finder's POST /search.

This is the discovery seam: everything that needs to find a table goes through
here, so the registry can be a separate process (or remote) rather than an
in-process call. Configure the finder with AGENT_FINDER_URL.
"""
import asyncio, os, json, socket, urllib.parse, urllib.request, urllib.error

import httpx

import runtime

BASE = os.getenv("AGENT_FINDER_URL", "http://127.0.0.1:8088").rstrip("/")
# Generous default: a rerank on a slow LOCAL model can take minutes; a too-short timeout was raising
# TimeoutError (a subclass of neither URLError nor ConnectionError), which escaped to the top -> HTTP 000.
TIMEOUT = int(os.getenv("AGENT_FINDER_TIMEOUT", "180"))

# Effective descriptors delivered by ARD, keyed by the OKF path used throughout the existing
# execution engine. This is populated at discovery time; local Markdown loading is now a fallback,
# not the normal request path.
_ENTRY_FRONTMATTER = {}


def _frontmatter_from_entry(item):
    """Mechanically compact one flattened ARD JSON-LD entry to its effective OKF mapping."""
    fm = {key[4:]: value for key, value in item.items()
          if key.startswith("okf:") and key not in ("okf:sourceDocument",
                                                     "okf:accessDescriptor")}
    if item.get("displayName") not in (None, ""):
        fm["title"] = item["displayName"]
    for key in ("description", "tags", "representativeQueries"):
        if item.get(key) not in (None, "", [], {}):
            fm[key] = item[key]
    if item.get("trustManifest"):
        fm["trust"] = item["trustManifest"]
    return fm


def cached_frontmatter(identifier):
    return _ENTRY_FRONTMATTER.get(identifier)


class DiscoveryError(RuntimeError):
    """The finder answered, but safe semantic discovery could not produce candidates."""


class RelevanceScoringError(DiscoveryError):
    pass


class NoRelevantTablesError(DiscoveryError):
    pass


# --- discovery usage ----------------------------------------------------------------------------
# The finder reports what each search cost it. Those calls belong to the finder, not to the caller's
# question, so they are accumulated SEPARATELY here and never folded into the caller's own ledger —
# Serving attaches this accumulator to QueryContext. This fallback exists only for offline clients.
_LEGACY_USAGE = None


class DiscoveryUsage:
    def __init__(self):
        self.searches = 0
        self.chat_calls = 0
        self.embed_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.embed_tokens = 0
        self.cost_usd = 0.0
        self.cost_is_reported = False
        self.by_model = {}

    def add(self, snap):
        if not isinstance(snap, dict):
            return
        self.searches += 1
        self.chat_calls += int(snap.get("chat_calls") or 0)
        self.embed_calls += int(snap.get("embed_calls") or 0)
        self.prompt_tokens += int(snap.get("prompt_tokens") or 0)
        self.completion_tokens += int(snap.get("completion_tokens") or 0)
        self.embed_tokens += int(snap.get("embed_tokens") or 0)
        self.cost_usd += float(snap.get("cost_usd") or 0.0)
        if snap.get("cost_source") == "provider":
            self.cost_is_reported = True
        for k, v in (snap.get("by_model") or {}).items():
            m = self.by_model.setdefault(k, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
            m["calls"] += int(v.get("calls") or 0)
            m["tokens"] += int(v.get("tokens") or 0)
            m["cost_usd"] += float(v.get("cost_usd") or 0.0)

    def snapshot(self):
        return {
            "searches": self.searches,
            "llm_calls": self.chat_calls + self.embed_calls,
            "chat_calls": self.chat_calls,
            "embed_calls": self.embed_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "embed_tokens": self.embed_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens + self.embed_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "cost_source": "provider" if self.cost_is_reported else "price-table",
            "by_model": {k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                         for k, v in self.by_model.items()},
            "billed_to": "agent-finder",        # a separate service: NOT part of the caller's total
        }


def start_usage():
    global _LEGACY_USAGE
    u = DiscoveryUsage()
    _LEGACY_USAGE = u
    return u


def bind_usage(u):
    global _LEGACY_USAGE
    _LEGACY_USAGE = u


def usage():
    return _LEGACY_USAGE


def create_async_http_client(**kwargs):
    """Create the application-owned client used by the async harness path.

    Ownership is deliberately outside this module: one Neural KG ASGI application will
    create one client at startup, place it on each QueryContext, and close it at shutdown. That
    avoids a client singleton tied to whichever event loop happened to call this module first.
    """
    kwargs.setdefault("timeout", TIMEOUT)
    return httpx.AsyncClient(**kwargs)


def _async_http_client(context):
    context.check()
    if context.http_client is None:
        raise RuntimeError("async Agent Finder access requires QueryContext.http_client")
    return context.http_client


def _record_async_usage(context, payload):
    if context.discovery_ledger is not None and isinstance(payload, dict):
        context.discovery_ledger.add(payload.get("usage"))


async def _arequest(context, method, path, *, params=None, json_body=None):
    """Make one cancellable Finder request through the caller-owned shared HTTPX client."""
    client = _async_http_client(context)
    remaining = context.remaining()
    timeout = TIMEOUT if remaining is None else min(TIMEOUT, remaining)
    try:
        response = await context.provider_call("finder", lambda: client.request(
            method, BASE + path, params=params, json=json_body, timeout=timeout))
    except (asyncio.CancelledError, runtime.QueryCancelled):
        raise
    except httpx.TimeoutException as exc:
        raise runtime.Refused(f"agent finder unreachable or too slow at {BASE} ({exc})") from exc
    except httpx.RequestError as exc:
        raise runtime.Refused(f"agent finder unreachable at {BASE} ({exc})") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    _record_async_usage(context, payload)
    return response, payload


async def _aget_async(path, *, context, params=None):
    response, payload = await _arequest(context, "GET", path, params=params)
    if response.status_code == 404:
        return None
    if response.is_error:
        raise runtime.Refused(f"agent finder error {response.status_code} for {path}")
    return payload


def _get(path, params=None):
    """GET against the Agent Finder. The registry is a service, so enumerating it goes over the
    same API as searching it rather than through the index file behind its back."""
    url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SystemExit(f"agent finder error {e.code} for {path}")
    except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, OSError) as e:
        raise SystemExit(f"agent finder unreachable at {BASE} ({e})")
    u = usage()                                  # the finder bills its own work; report separately
    if u is not None and isinstance(payload, dict):
        u.add(payload.get("usage"))
    return payload


def agents(publisher=None, q="", page_size=50, page_token=""):
    """GET /agents — the ARD list endpoint: catalog entries, filtered and paginated."""
    params = {"pageSize": page_size}
    if publisher:
        params["publisher"] = publisher
    if q:
        params["q"] = q
    if page_token:
        params["pageToken"] = page_token
    return _get("/agents", params) or {"entries": [], "totalSize": 0, "pageToken": None}


def entry(identifier):
    """GET /agents/entry — one self-contained, mechanically projected OKF ARD entry."""
    return _get("/agents/entry", {"id": identifier})


def explore(field="publisher", limit=100):
    """POST /explore — facet counts, e.g. how many tables each publisher contributes."""
    body = json.dumps({"query": {"text": ""},
                       "resultType": {"facets": [{"field": field, "limit": limit}]}}).encode()
    req = urllib.request.Request(BASE + "/explore", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except Exception:
        return {"facets": {}}


def manifest():
    """GET /.well-known/ard.json — the ARD capability manifest."""
    return _get("/.well-known/ard.json") or {}


def health():
    """Cost-free finder readiness. This endpoint must never perform semantic search or embedding."""
    return _get("/healthz") or {"ok": False}


async def agents_async(publisher=None, q="", page_size=50, page_token="", *, context):
    params = {"pageSize": page_size}
    if publisher:
        params["publisher"] = publisher
    if q:
        params["q"] = q
    if page_token:
        params["pageToken"] = page_token
    return await _aget_async("/agents", context=context, params=params) or {
        "entries": [], "totalSize": 0, "pageToken": None}


async def entry_async(identifier, *, context):
    return await _aget_async("/agents/entry", context=context, params={"id": identifier})


async def explore_async(field="publisher", limit=100, *, context):
    body = {"query": {"text": ""},
            "resultType": {"facets": [{"field": field, "limit": limit}]}}
    try:
        response, payload = await _arequest(context, "POST", "/explore", json_body=body)
        return payload if not response.is_error else {"facets": {}}
    except (asyncio.CancelledError, runtime.QueryCancelled):
        raise
    except runtime.Refused:
        return {"facets": {}}


async def manifest_async(*, context):
    return await _aget_async("/.well-known/ard.json", context=context) or {}


async def health_async(*, context):
    return await _aget_async("/healthz", context=context) or {"ok": False}


def search_many(texts, k=10, sources=None, rerank=True, rerank_query=None):
    """One finder request for several phrasings; the finder embeds them together and reranks once."""
    texts = list(dict.fromkeys(str(text).strip() for text in texts if str(text).strip()))
    if not texts:
        return []
    body = json.dumps({"query": {"text": rerank_query or texts[0], "texts": texts},
                       "pageSize": k, "sources": sources, "rerank": rerank}).encode()
    req = urllib.request.Request(BASE + "/search", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.load(r)
        u = usage()
        if u is not None:
            u.add(payload.get("usage"))             # reported, not charged to the caller
    except urllib.error.HTTPError as e:
        try:
            payload = json.load(e)
        except Exception:
            payload = {}
        u = usage()
        if u is not None:
            u.add(payload.get("usage"))
        if payload.get("code") == "relevance_scoring_failed":
            raise RelevanceScoringError(payload.get("error") or
                                        "table relevance scoring is temporarily unavailable") from e
        raise SystemExit(f"agent finder error {e.code}: "
                         f"{payload.get('error') or e.reason}") from e
    except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, OSError) as e:
        raise SystemExit(f"agent finder unreachable or too slow at {BASE} ({e}). "
                         f"Start it (python3 agent_finder.py); for slow local models raise "
                         f"AGENT_FINDER_TIMEOUT.")
    return _search_results(payload)


def search(text, k=10, sources=None, rerank=True):
    return search_many([text], k=k, sources=sources, rerank=rerank, rerank_query=text)


def _search_results(payload):
    eligibility = payload.get("eligibility") or {}
    if eligibility.get("status") == "no_match":
        top, threshold = eligibility.get("topScore"), eligibility.get("threshold")
        observed = "no valid score was returned" if top is None else f"top score {top:g}"
        raise NoRelevantTablesError(
            f"no indexed table cleared the LLM relevance threshold ({observed}; "
            f"threshold {threshold:g}); nothing was fetched")
    results = []
    for item in payload.get("results", []):
        identifier = item.get("okf:sourceDocument") or item["identifier"]
        metadata = _frontmatter_from_entry(item)
        _ENTRY_FRONTMATTER[identifier] = metadata
        parts = identifier.split("/")
        results.append({"identifier": identifier, "urn": item["identifier"],
                        "title": item.get("displayName", ""), "score": item.get("score"),
                        "publisher": parts[1] if len(parts) > 2 else
                                     (item.get("tags") or [None])[0],
                        "metadata": metadata})
    return results


async def search_many_async(texts, k=10, sources=None, rerank=True, rerank_query=None, *, context):
    """Async Finder discovery through the shared client carried by QueryContext."""
    texts = list(dict.fromkeys(str(text).strip() for text in texts if str(text).strip()))
    if not texts:
        return []
    body = {"query": {"text": rerank_query or texts[0], "texts": texts},
            "pageSize": k, "sources": sources, "rerank": rerank}
    response, payload = await _arequest(context, "POST", "/search", json_body=body)
    if response.is_error:
        if payload.get("code") == "relevance_scoring_failed":
            raise RelevanceScoringError(payload.get("error") or
                                        "table relevance scoring is temporarily unavailable")
        if payload.get("code") == "query_cancelled":
            context.cancel()
            raise runtime.QueryCancelled(payload.get("error") or "Agent Finder query cancelled")
        raise runtime.Refused(f"agent finder error {response.status_code}: "
                         f"{payload.get('error') or response.reason_phrase}")
    return _search_results(payload)


async def search_async(text, k=10, sources=None, rerank=True, *, context):
    return await search_many_async([text], k=k, sources=sources, rerank=rerank,
                                   rerank_query=text, context=context)
