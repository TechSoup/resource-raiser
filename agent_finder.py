#!/usr/bin/env python3
"""ARD Agent Finder — an in-memory registry that serves the OKF data tables.

Implements the ARD discovery contract over HTTP:
  POST /search   {"query": {"text": "..."}, "pageSize": N}
                 -> {"results": [{identifier, displayName, type, source, score}], ...}
  POST /search   {"query": {"text": "rerank wording", "texts": ["phrase 1", "phrase 2"]}, ...}
                 -> embeds the phrasings together, unions retrieval, and reranks once
  GET  /         service card

The store is the embedded index built by registry/index.py (SEC + Treasury
tables). Run with the Azure keys loaded:
  set -a; source ./set_keys.sh; set +a
  python3 agent_finder.py            # serves on http://127.0.0.1:8088
"""
import asyncio, base64, json, os, re, sys, time, urllib.parse
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import llm, runtime
from query_context import QueryContext
from registry import index

PORT = int(os.getenv("AGENT_FINDER_PORT", "8088"))
# Bind loopback by DEFAULT: this service has no auth, and an embedding index that answers anyone
# who asks is not something to expose by accident. A deployment that needs it reachable sets
# BIND_HOST explicitly (0.0.0.0 behind a reverse proxy / private network).
HOST = os.getenv("AGENT_FINDER_BIND_HOST", "127.0.0.1")
SELF = os.getenv("AGENT_FINDER_SELF") or f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '::') else HOST}:{PORT}/"


# --- public exposure ----------------------------------------------------------------------------
# Listing, faceting and the manifest are in-memory reads and cost nothing. POST /search does not:
# it embeds the query and (unless rerank is off) runs an LLM over the candidates, so every call
# spends real credits. Exposed publicly that is an open tab on someone else's card, which is why
# the cap applies to /search specifically rather than to the whole service.
SEARCH_LIMIT_PER_DAY = int(os.getenv("SEARCH_LIMIT_PER_DAY", "500"))     # 0 disables
TRUST_PROXY = os.getenv("TRUST_PROXY", "0").lower() in ("1", "true", "yes")
_QUOTA = {}  # event-loop-owned; quota_ok contains no await, so each update is one critical section


def client_ip(request):
    """Who to bill a search to. X-Forwarded-For is client-supplied, so it is only believed when a
    proxy we control is declared — otherwise anyone could mint a fresh quota per request."""
    if TRUST_PROXY:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            ip = xff.split(",")[-1].strip()
            if ip.count(":") == 1:
                ip = ip.split(":")[0]
            if ip:
                return ip
    return request.client.host if request.client else "unknown"


def quota_ok(ip):
    if SEARCH_LIMIT_PER_DAY <= 0:
        return True, 0
    now = time.time()
    day, reset = int(now // 86400), int((int(now // 86400) + 1) * 86400 - now)
    rec = _QUOTA.get(ip)
    if rec is None or rec[0] != day:
        if len(_QUOTA) > 50_000:
            for key in [key for key, value in _QUOTA.items() if value[0] != day]:
                _QUOTA.pop(key, None)
        rec = [day, 0]
        _QUOTA[ip] = rec
    if rec[1] >= SEARCH_LIMIT_PER_DAY:
        return False, reset
    rec[1] += 1
    return True, reset


def publisher(identifier):
    # sources/<source-dir>/<table>.md  ->  the source directory
    parts = identifier.split("/")
    return parts[1] if len(parts) > 2 else "root"



# --- the catalog behind the ARD endpoints -------------------------------------------------------
# /search answers "which table fits this question". A registry also has to answer "what IS in
# here" — which the ARD spec covers with the optional GET /agents (list) and POST /explore
# (facets) alongside the well-known catalog manifest. Serving them here keeps the index behind ONE
# service: a browser or another agent enumerates the registry the same way it searches it, instead
# of reaching around the API into registry/meta.json.
_ENTRIES = None
_BY_PUBLISHER = None
_BY_URN = None
ROOT = os.path.dirname(os.path.abspath(__file__))

# ARD v0.91 terms resolve against the base context, which a conformant consumer applies as the
# JSON-LD expandContext. Carrying @context on the wire is optional; we send it because these
# entries also declare a SECOND namespace, and a prefixed term is only meaningful if its prefix
# is bound somewhere the reader can see.
ARD_CONTEXT = os.getenv("ARD_CONTEXT_URL", "https://agenticresourcediscovery.org/context/v1")

# The OKF namespace. OKF has not published a term IRI, so this is provisional and overridable —
# it is a stable identifier for "the Open Knowledge Format vocabulary", anchored on where OKF
# actually lives rather than on a domain nobody has claimed. Swap it the day OKF mints one; the
# term names below do not change.
OKF_NS = os.getenv("OKF_NAMESPACE",
                   "https://github.com/GoogleCloudPlatform/knowledge-catalog/okf/ns#")

# OKF frontmatter -> namespaced ARD term. These are exactly the fields ARD's own vocabulary has
# no word for: what taxonomy a measure comes from, which concept it pins, what one row means.
# The schema keeps additionalProperties open so they stay valid and become filter dimensions.
_OKF_TERMS = {
    "taxonomy": "okf:taxonomy", "concept": "okf:concept", "periodType": "okf:periodType",
    "unit": "okf:unit", "variable": "okf:variable", "field": "okf:field",
    "measureid": "okf:measureId", "scorecard": "okf:scorecardField", "tfield": "okf:treasuryField",
    "path": "okf:datasetPath", "get": "okf:queryParams", "filter": "okf:filter",
    "entityType": "okf:entityType", "type": "okf:documentType",
}


def _urn_segment(v):
    """A URN segment: the pattern allows [A-Za-z0-9._-] only."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(v or "")).strip("-") or "unknown"


def _publisher_authority(fm_access, fallback):
    """The <publisher> segment of the URN — the authority anchor, which MUST align with the trust
    domain. Some sources name two publishers in prose ("census.gov / Google BigQuery public
    datasets"); the domain is the part that anchors authority, so take that."""
    pub = (fm_access.get("publisher") or "").strip()
    m = re.search(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", pub, re.I)
    if m:
        return _urn_segment(m.group(0))
    ident = ((fm_access.get("trust") or {}).get("identity") or "")
    m = re.search(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", ident, re.I)
    return _urn_segment(m.group(0) if m else fallback)


_ACCESS_CACHE = {}


def _access_fm(source_dir):
    if source_dir not in _ACCESS_CACHE:
        import yaml
        p = os.path.join(ROOT, "sources", source_dir, "_access.md")
        try:
            with open(p, encoding="utf-8") as stream:
                t = stream.read()
            _ACCESS_CACHE[source_dir] = yaml.safe_load(t.split("---", 2)[1]) or {}
        except Exception:
            _ACCESS_CACHE[source_dir] = {}
    return _ACCESS_CACHE[source_dir]


def _leaf_fm(identifier):
    import yaml
    p = os.path.join(ROOT, identifier)
    try:
        with open(p, encoding="utf-8") as stream:
            t = stream.read()
        return yaml.safe_load(t.split("---", 2)[1]) or {}
    except Exception:
        return {}


def ard_urn(identifier):
    """urn:air:<publisher>:okf:<source>.<leaf> — domain-anchored, per §4.2."""
    src = publisher(identifier)
    leaf = os.path.splitext(os.path.basename(identifier))[0]
    auth = _publisher_authority(_access_fm(src), src)
    return f"urn:air:{auth}:okf:{_urn_segment(src)}.{_urn_segment(leaf)}"


def _entry_from_meta(m, full=False):
    """An index record as an ARD v0.91 entry.

    `full=False` yields an ardEntryProjection (search/list shape). `full=True` yields an ardEntry,
    which requires exactly one of url/data — so the OKF document travels inline as `data` and the
    projection carries `url` instead. Sending both would violate the oneOf.
    """
    identifier = m["identifier"]
    src = publisher(identifier)
    acc = _access_fm(src)
    e = {
        # FIRST key, and on EVERY entry — not just full ones, and not only on the response
        # envelope. These entries use a second namespace, and a prefixed term is undefined unless
        # its prefix is bound in scope. An envelope-only context holds while the entry sits in the
        # document and breaks the moment a consumer lifts one entry out of `entries[]` — which is
        # exactly what a registry ingesting them does.
        "@context": [ARD_CONTEXT, {"okf": OKF_NS}],
        "identifier": ard_urn(identifier),
        "displayName": m.get("title", ""),
        "type": "application/okf-table+markdown",
        "description": m.get("description", ""),
        "representativeQueries": (m.get("queries") or [])[:6],
        "tags": [src],
        # the document path this entry was generated from — the handle every OKF tool already uses
        "okf:sourceDocument": identifier,
        "okf:source": src,
        "okf:accessDescriptor": f"sources/{src}/_access.md",
    }
    if m.get("scope"):
        e["okf:entityType"] = m["scope"]
    trust = acc.get("trust") or {}
    if trust.get("identity"):
        e["trustManifest"] = {k: v for k, v in trust.items() if v}
    if full:
        fm = _leaf_fm(identifier)
        for k, term in _OKF_TERMS.items():
            if fm.get(k) not in (None, "", [], {}):
                e[term] = fm[k]
        e["data"] = {"mediaType": "text/markdown", "frontmatter": fm,
                     "content": _leaf_text(identifier)}
    else:
        e["url"] = f"{SELF.rstrip('/')}/agents/entry?id={urllib.parse.quote(identifier)}"
    return e


def _leaf_text(identifier):
    root = os.path.realpath(os.path.join(ROOT, "sources"))
    path = os.path.realpath(os.path.join(ROOT, identifier))
    if not path.startswith(root + os.sep) or not path.endswith(".md") or not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def _catalog():
    """Every leaf as an ARD entry projection, indexed by publisher and by URN."""
    global _ENTRIES, _BY_PUBLISHER, _BY_URN
    if _ENTRIES is None:
        _ENTRIES, _BY_PUBLISHER, _BY_URN = [], {}, {}
        with open(index.CACHE_META) as stream:
            metadata = json.load(stream)
        for m in metadata:
            e = _entry_from_meta(m)
            e["_path"] = m["identifier"]                  # internal: not emitted on the wire
            _ENTRIES.append(e)
            _BY_PUBLISHER.setdefault(publisher(m["identifier"]), []).append(e)
            _BY_URN[e["identifier"]] = m["identifier"]
        for v in _BY_PUBLISHER.values():
            v.sort(key=lambda e: e["displayName"] or e["identifier"])
    return _ENTRIES, _BY_PUBLISHER


def _wire(e):
    return {k: v for k, v in e.items() if not k.startswith("_")}


def _token(offset):
    """Opaque pageToken, as the spec asks for — a cursor, not a page number the caller does math on."""
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _offset(token):
    if not token:
        return 0
    try:
        pad = "=" * (-len(token) % 4)
        v = base64.urlsafe_b64decode(token + pad).decode()
        return max(0, int(v.split(":", 1)[1]))
    except Exception:
        return 0


def _agents(qs):
    """GET /agents — list catalog entries. `filter` takes `publisher=<dir>` and/or a free-text
    `q=<text>`; paginated with pageSize / pageToken."""
    entries, by_pub = _catalog()
    pub = (qs.get("publisher") or [""])[0] or ""
    filt = (qs.get("filter") or [""])[0]
    if not pub and filt.startswith("publisher="):
        pub = filt.split("=", 1)[1].strip().strip('"')
    items = by_pub.get(pub, []) if pub else entries
    q = ((qs.get("q") or [""])[0] or "").lower()
    if q:
        items = [e for e in items
                 if q in (e.get("displayName") or "").lower()
                 or q in (e.get("description") or "").lower()
                 or q in e["identifier"].lower() or q in (e.get("_path") or "").lower()
                 or any(q in x.lower() for x in (e.get("representativeQueries") or []))]
    try:
        size = int((qs.get("pageSize") or ["50"])[0] or 50)
    except (TypeError, ValueError):
        size = 50
    size = max(1, min(size, 100))   # spec: max 100
    off = _offset((qs.get("pageToken") or [""])[0])
    page = items[off:off + size]
    nxt = _token(off + size) if off + size < len(items) else None
    return {"@context": [ARD_CONTEXT, {"okf": OKF_NS}],
            "entries": [_wire(e) for e in page], "totalSize": len(items), "pageSize": size,
            "pageToken": nxt, "offset": off}


def _explore(req):
    """POST /explore — facet counts. Answers 'what publishers exist and how big is each',
    which is what a catalog browser needs before it can list anything."""
    _entries, by_pub = _catalog()
    limit = 100
    for f in ((req.get("resultType") or {}).get("facets") or []):
        if f.get("field") in ("publisher", "source"):
            try:
                limit = max(1, min(int(f.get("limit") or 100), 1000))
            except (TypeError, ValueError):
                limit = 100
    buckets = sorted(({"value": k, "count": len(v)} for k, v in by_pub.items()),
                     key=lambda b: -b["count"])
    return {"resultType": "facets",
            "facets": {"publisher": {"buckets": buckets[:limit],
                                     "otherCount": max(0, len(buckets) - limit)}}}


def _entry(identifier):
    """A full ARD entry. Accepts either the URN or the OKF document path, because the two name the
    same thing and a client that listed entries has the URN while an OKF tool has the path."""
    _entries, _by = _catalog()
    path = _BY_URN.get(identifier, identifier)
    root = os.path.realpath(os.path.join(ROOT, "sources"))
    real = os.path.realpath(os.path.join(ROOT, path))
    if not real.startswith(root + os.sep) or not real.endswith(".md") or not os.path.exists(real):
        return None
    hit = next((e for e in _entries if e["_path"] == path), None)
    if hit:
        return _wire(_entry_from_meta({"identifier": path, "title": hit["displayName"],
                                       "description": hit.get("description", ""),
                                       "queries": hit.get("representativeQueries") or [],
                                       "scope": hit.get("okf:entityType", "")}, full=True))
    # not a leaf — a source's _access.md, the document its leaves inherit endpoint and operations
    # from. Still an ARD entry, of a different media type.
    fm = _leaf_fm(path)
    src = publisher(path)
    acc = _access_fm(src)
    e = {"@context": [ARD_CONTEXT, {"okf": OKF_NS}],
         "identifier": f"urn:air:{_publisher_authority(acc, src)}:okf:{_urn_segment(src)}",
         "displayName": fm.get("title", path), "type": "application/okf-source+markdown",
         "description": fm.get("description", ""),
         "representativeQueries": fm.get("representativeQueries") or [],
         "tags": [src], "okf:sourceDocument": path, "okf:source": src,
         "data": {"mediaType": "text/markdown", "frontmatter": fm, "content": _leaf_text(path)}}
    if fm.get("entityType"):
        e["okf:entityType"] = fm["entityType"]
    if (acc.get("trust") or {}).get("identity"):
        e["trustManifest"] = {k: v for k, v in acc["trust"].items() if v}
    if acc.get("access"):
        e["okf:access"] = acc["access"]
    return e


def _manifest():
    """GET /.well-known/ard.json — an ardManifest. ARD requires only `entries`; the rest is
    transport-defined and ignored by conformant consumers.

    The entries listed are the SOURCES, not the 8,925 leaves: a manifest is meant to be fetched
    whole, and the per-table entries are what /agents and /search are for.
    """
    _entries, by_pub = _catalog()
    entries = []
    for src in sorted(by_pub, key=lambda k: -len(by_pub[k])):
        acc = _access_fm(src)
        auth = _publisher_authority(acc, src)
        e = {"@context": [ARD_CONTEXT, {"okf": OKF_NS}],
             "identifier": f"urn:air:{auth}:okf:{_urn_segment(src)}",
             "displayName": (acc.get("title") or src).replace(" (access)", ""),
             "type": "application/okf-source+markdown",
             "description": acc.get("description", "") or acc.get("entityType", ""),
             "url": f"{SELF.rstrip('/')}/agents/entry?id=" +
                    urllib.parse.quote(f"sources/{src}/_access.md"),
             "representativeQueries": (acc.get("representativeQueries") or [])[:5],
             "tags": [src], "okf:source": src, "okf:tableCount": len(by_pub[src])}
        if acc.get("entityType"):
            e["okf:entityType"] = acc["entityType"]
        if (acc.get("trust") or {}).get("identity"):
            e["trustManifest"] = {k: v for k, v in acc["trust"].items() if v}
        entries.append(e)
    return {
        "@context": [ARD_CONTEXT, {"okf": OKF_NS}],
        "specVersion": "0.91",
        "host": {"name": "Resource Raiser — ARD Agent Finder", "url": SELF,
                 "description": "OKF data-table descriptors for ~20 authoritative US public data "
                                "sources, discoverable by natural-language query."},
        "capabilities": {"search": "POST /search", "explore": "POST /explore",
                         "list": "GET /agents", "entry": "GET /agents/entry?id="},
        "entries": entries,
        "okf:tableCount": len(_entries),
    }


FINDER_QUERY_TIMEOUT = float(os.getenv("AGENT_FINDER_QUERY_TIMEOUT", "180"))
_MAX_BODY = int(os.getenv("AGENT_FINDER_MAX_BODY", "65536"))


def _json(payload, status=200, headers=None):
    return JSONResponse(payload, status_code=status, headers=headers)


async def _body(request):
    raw_length = request.headers.get("content-length")
    try:
        length = int(raw_length) if raw_length is not None else 0
    except (TypeError, ValueError):
        return None, _json({"error": "invalid Content-Length"}, 400)
    if length < 0 or length > _MAX_BODY:
        return None, _json({"error": f"request body exceeds {_MAX_BODY} bytes"}, 413)
    try:
        raw = await request.body()
    except Exception:
        return None, _json({"error": "could not read request body"}, 400)
    if len(raw) > _MAX_BODY:
        return None, _json({"error": f"request body exceeds {_MAX_BODY} bytes"}, 413)
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None, _json({"error": "invalid JSON body"}, 400)
    if not isinstance(payload, dict):
        return None, _json({"error": "JSON body must be an object"}, 400)
    return payload, None


async def root(_request):
    return _json({"name": "ARD Agent Finder — OKF data tables", "store": "in-memory",
                  "endpoints": {"search": "POST /search", "explore": "POST /explore",
                                "list": "GET /agents",
                                "entry": "GET /agents/entry?id=",
                                "manifest": "GET /.well-known/ard.json"}})


async def healthz(_request):
    try:
        vecs, meta = index._store()
        ok = len(vecs) == len(meta) and len(meta) > 0 and len(vecs.shape) == 2
        return _json({"ok": ok, "entries": len(meta),
                      "dimensions": int(vecs.shape[1]) if ok else 0}, 200 if ok else 503)
    except Exception as exc:
        return _json({"ok": False, "error": type(exc).__name__}, 503)


async def manifest_endpoint(_request):
    return _json(_manifest())


async def agents_endpoint(request):
    params = {}
    for key, value in request.query_params.multi_items():
        params.setdefault(key, []).append(value)
    return _json(_agents(params))


async def entry_endpoint(request):
    entry = _entry(request.query_params.get("id", ""))
    return _json(entry) if entry else _json({"error": "no such entry"}, 404)


async def explore_endpoint(request):
    payload, error = await _body(request)
    return error or _json(_explore(payload))


async def search_endpoint(request):
    payload, error = await _body(request)
    if error:
        return error
    ok, reset = quota_ok(client_ip(request))
    if not ok:
        return _json({"error": f"daily search limit reached ({SEARCH_LIMIT_PER_DAY}/day per source)",
                      "retryAfterSeconds": reset}, 429, {"Retry-After": str(reset)})
    query = payload.get("query") or {}
    if not isinstance(query, dict) or not isinstance(query.get("text"), str) \
            or not query["text"].strip():
        return _json({"error": "query.text must be a non-empty string"}, 400)
    text = query["text"].strip()
    texts = query.get("texts") or [text]
    if (not isinstance(texts, list) or len(texts) > 4
            or not all(isinstance(item, str) and item.strip() for item in texts)):
        return _json({"error": "query.texts must be a list of 1-4 non-empty strings"}, 400)
    texts = [item.strip() for item in texts]
    try:
        k = int(payload.get("pageSize", 10))
    except (TypeError, ValueError):
        return _json({"error": "pageSize must be an integer"}, 400)
    k = max(1, min(k, 100))

    ledger = llm.Ledger()
    context = QueryContext.with_timeout(
        FINDER_QUERY_TIMEOUT, usage_ledger=ledger,
        llm_client=request.app.state.llm_client or llm.async_client())
    try:
        matches = await index.search_many_async(
            texts, k, sources=payload.get("sources"), rerank=payload.get("rerank", True),
            rerank_query=text, context=context)
    except asyncio.CancelledError:
        context.cancel()
        raise
    except runtime.QueryCancelled as exc:
        return _json({"code": "query_cancelled", "error": str(exc), "usage": ledger.snapshot()}, 504)
    except index.RelevanceScoringError:
        return _json({
            "code": "relevance_scoring_failed",
            "error": ("table relevance scoring is temporarily unavailable; embedding "
                      "similarity was not used as a substitute"),
            "usage": ledger.snapshot(),
        }, 503)
    except index.NoRelevantTablesError as exc:
        return _json({
            "@context": [ARD_CONTEXT, {"okf": OKF_NS}],
            "results": [], "referrals": [], "pageToken": None,
            "eligibility": {"status": "no_match", "threshold": exc.threshold,
                            "topScore": exc.top_score},
            "usage": ledger.snapshot(),
        })

    results = []
    for hit in matches:
        entry = _entry_from_meta({"identifier": hit["identifier"], "title": hit["title"],
                                  "description": hit.get("description", ""),
                                  "queries": hit.get("queries") or []})
        entry.update({"score": hit["score"], "source": SELF})
        results.append(_wire(entry))
    return _json({"@context": [ARD_CONTEXT, {"okf": OKF_NS}],
                  "results": results, "referrals": [], "pageToken": None,
                  "usage": ledger.snapshot()})


def create_app(llm_client=None):
    @asynccontextmanager
    async def lifespan(application):
        # Immutable release artifacts are loaded before readiness, never on a request.
        index._store()
        _catalog()
        owns_client = llm_client is None
        application.state.llm_client = llm_client or llm.async_client()
        try:
            yield
        finally:
            if owns_client:
                await llm.close_async_client()

    routes = [
        Route("/", root),
        Route("/healthz", healthz),
        Route("/healthz/", healthz),
        Route("/.well-known/ard.json", manifest_endpoint),
        Route("/.well-known/ard.json/", manifest_endpoint),
        Route("/agents/entry", entry_endpoint),
        Route("/agents/entry/", entry_endpoint),
        Route("/agents", agents_endpoint),
        Route("/agents/", agents_endpoint),
        Route("/explore", explore_endpoint, methods=["POST"]),
        Route("/explore/", explore_endpoint, methods=["POST"]),
        Route("/search", search_endpoint, methods=["POST"]),
        Route("/search/", search_endpoint, methods=["POST"]),
    ]
    middleware = [Middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"], expose_headers=["Retry-After"], max_age=86400)]
    application = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    application.state.llm_client = llm_client
    return application


app = create_app()


if __name__ == "__main__":
    release_ok, release_detail = index.verify(require_release=True)
    if not release_ok:
        print("ERROR: registry release is stale or incomplete", file=sys.stderr)
        for error in release_detail.get("errors", [release_detail.get("error", "unknown error")]):
            print(f"  - {error}", file=sys.stderr)
        print(f"Run: {sys.executable} tools/build_registry_release.py", file=sys.stderr)
        raise SystemExit(1)
    with open(index.CACHE_META) as stream:
        table_count = len(json.load(stream))
    print(f"ARD Agent Finder on {SELF} (bind {HOST}:{PORT})  (POST /search) — "
          f"{table_count} tables")
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, workers=1)
