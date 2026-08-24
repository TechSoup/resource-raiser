#!/usr/bin/env python3
"""Thin ARD client — calls a remote Agent Finder's POST /search.

This is the discovery seam: everything that needs to find a table goes through
here, so the registry can be a separate process (or remote) rather than an
in-process call. Configure the finder with AGENT_FINDER_URL.
"""
import os, json, socket, urllib.parse, urllib.request, urllib.error, threading

BASE = os.getenv("AGENT_FINDER_URL", "http://127.0.0.1:8088").rstrip("/")
# Generous default: a rerank on a slow LOCAL model can take minutes; a too-short timeout was raising
# TimeoutError (a subclass of neither URLError nor ConnectionError), which escaped to the top -> HTTP 000.
TIMEOUT = int(os.getenv("AGENT_FINDER_TIMEOUT", "180"))


# --- discovery usage ----------------------------------------------------------------------------
# The finder reports what each search cost it. Those calls belong to the finder, not to the caller's
# question, so they are accumulated SEPARATELY here and never folded into the caller's own ledger —
# a question makes several searches, and fan-out stages search from worker threads, so like the
# harness ledger this is one shared object rather than a per-thread counter.
_TALLY = threading.local()


class DiscoveryUsage:
    def __init__(self):
        self._lock = threading.Lock()
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
        with self._lock:
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
        with self._lock:
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
    u = DiscoveryUsage()
    _TALLY.u = u
    return u


def bind_usage(u):
    _TALLY.u = u                                    # always assign: pool threads are reused


def usage():
    return getattr(_TALLY, "u", None)


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
    """GET /agents/entry — one catalog entry with the OKF document inline as `data`."""
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
        results = payload.get("results", [])
        u = usage()
        if u is not None:
            u.add(payload.get("usage"))             # reported, not charged to the caller
    except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, OSError) as e:
        raise SystemExit(f"agent finder unreachable or too slow at {BASE} ({e}). "
                         f"Start it (python3 agent_finder.py); for slow local models raise "
                         f"AGENT_FINDER_TIMEOUT or set ARD_RERANK=0.")
    # ARD v0.91 identifiers are URNs. Everything downstream addresses a table by its OKF document
    # path (that is what the planner, the accessor and the fetchers all take), so map the wire form
    # back here — this client is the seam between the protocol and the engine.
    return [{"identifier": x.get("okf:sourceDocument") or x["identifier"],
             "urn": x["identifier"],
             "title": x.get("displayName", ""),
             "score": x.get("score"),
             "publisher": x.get("okf:source") or (x.get("tags") or [None])[0]}
            for x in results]


def search(text, k=10, sources=None, rerank=True):
    return search_many([text], k=k, sources=sources, rerank=rerank, rerank_query=text)
