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
import base64, json, os, re, signal, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
import llm
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
_QUOTA, _QUOTA_LOCK = {}, threading.Lock()


def client_ip(handler):
    """Who to bill a search to. X-Forwarded-For is client-supplied, so it is only believed when a
    proxy we control is declared — otherwise anyone could mint a fresh quota per request."""
    if TRUST_PROXY:
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff:
            ip = xff.split(",")[-1].strip()
            if ip.count(":") == 1:
                ip = ip.split(":")[0]
            if ip:
                return ip
    return handler.client_address[0]


def quota_ok(ip):
    if SEARCH_LIMIT_PER_DAY <= 0:
        return True, 0
    now = time.time()
    day, reset = int(now // 86400), int((int(now // 86400) + 1) * 86400 - now)
    with _QUOTA_LOCK:
        rec = _QUOTA.get(ip)
        if rec is None or rec[0] != day:
            if len(_QUOTA) > 50_000:
                for k in [k for k, v in _QUOTA.items() if v[0] != day]:
                    _QUOTA.pop(k, None)
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
            t = open(p, encoding="utf-8").read()
            _ACCESS_CACHE[source_dir] = yaml.safe_load(t.split("---", 2)[1]) or {}
        except Exception:
            _ACCESS_CACHE[source_dir] = {}
    return _ACCESS_CACHE[source_dir]


def _leaf_fm(identifier):
    import yaml
    p = os.path.join(ROOT, identifier)
    try:
        t = open(p, encoding="utf-8").read()
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
    return open(path, encoding="utf-8").read()


def _catalog():
    """Every leaf as an ARD entry projection, indexed by publisher and by URN."""
    global _ENTRIES, _BY_PUBLISHER, _BY_URN
    if _ENTRIES is None:
        _ENTRIES, _BY_PUBLISHER, _BY_URN = [], {}, {}
        for m in json.load(open(index.CACHE_META)):
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


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        # A discovery registry is a public read surface; without these a browser-based agent
        # cannot call it cross-origin at all.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path.rstrip("/"), urllib.parse.parse_qs(u.query)
        if p in ("", "/"):
            return self._json(200, {"name": "ARD Agent Finder — OKF data tables",
                                    "store": "in-memory",
                                    "endpoints": {"search": "POST /search", "explore": "POST /explore",
                                                  "list": "GET /agents",
                                                  "entry": "GET /agents/entry?id=",
                                                  "manifest": "GET /.well-known/ard.json"}})
        if p == "/healthz":
            # Cost-free liveness/readiness: validate the already-built artifacts without embedding
            # a probe query, spending provider credits, or consuming the /search quota.
            try:
                vecs, meta = index._store()
                ok = len(vecs) == len(meta) and len(meta) > 0 and len(vecs.shape) == 2
                return self._json(200 if ok else 503,
                                  {"ok": ok, "entries": len(meta),
                                   "dimensions": int(vecs.shape[1]) if ok else 0})
            except Exception as e:
                return self._json(503, {"ok": False, "error": type(e).__name__})
        if p == "/.well-known/ard.json":
            return self._json(200, _manifest())
        if p == "/agents/entry":
            e = _entry((qs.get("id") or [""])[0])
            return self._json(200, e) if e else self._json(404, {"error": "no such entry"})
        if p == "/agents":
            return self._json(200, _agents(qs))
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path not in ("/search", "/explore"):
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return self._json(400, {"error": "invalid Content-Length"})
        max_body = int(os.getenv("AGENT_FINDER_MAX_BODY", "65536"))
        if n < 0 or n > max_body:
            return self._json(413, {"error": f"request body exceeds {max_body} bytes"})
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid JSON body"})
        if not isinstance(req, dict):
            return self._json(400, {"error": "JSON body must be an object"})
        if path == "/explore":
            return self._json(200, _explore(req))
        ok, reset = quota_ok(client_ip(self))
        if not ok:
            return self._json(429, {"error": f"daily search limit reached "
                                             f"({SEARCH_LIMIT_PER_DAY}/day per source)",
                                    "retryAfterSeconds": reset})
        query = req.get("query") or {}
        if not isinstance(query, dict) or not isinstance(query.get("text"), str) or not query["text"].strip():
            return self._json(400, {"error": "query.text must be a non-empty string"})
        text = query["text"].strip()
        texts = query.get("texts") or [text]
        if (not isinstance(texts, list) or len(texts) > 4 or
                not all(isinstance(item, str) and item.strip() for item in texts)):
            return self._json(400, {"error": "query.texts must be a list of 1-4 non-empty strings"})
        texts = [item.strip() for item in texts]
        try:
            k = int(req.get("pageSize", 10))
        except (TypeError, ValueError):
            return self._json(400, {"error": "pageSize must be an integer"})
        k = max(1, min(k, 100))
        # Discovery's query embedding and its LLM re-rank are billed HERE, in this service. They
        # are reported per search so a caller can see what discovery cost — the finder has its own
        # lifecycle, so this is not part of any one question's bill.
        led = llm.start_ledger()
        # results are ardEntryProjections: only `identifier` is required, and `score`/`source`
        # ride alongside as the transport's own annotations.
        results = []
        try:
            matches = index.search_many(texts, k, sources=req.get("sources"),
                                        rerank=req.get("rerank", True), rerank_query=text)
        except index.RelevanceScoringError:
            return self._json(503, {
                "code": "relevance_scoring_failed",
                "error": ("table relevance scoring is temporarily unavailable; embedding "
                          "similarity was not used as a substitute"),
                "usage": led.snapshot(),
            })
        except index.NoRelevantTablesError as e:
            return self._json(200, {
                "@context": [ARD_CONTEXT, {"okf": OKF_NS}],
                "results": [], "referrals": [], "pageToken": None,
                "eligibility": {"status": "no_match", "threshold": e.threshold,
                                "topScore": e.top_score},
                "usage": led.snapshot(),
            })
        for h in matches:
            e = _entry_from_meta({"identifier": h["identifier"], "title": h["title"],
                                  "description": h.get("description", ""),
                                  "queries": h.get("queries") or []})
            e.update({"score": h["score"], "source": SELF})
            results.append(_wire(e))
        self._json(200, {"@context": [ARD_CONTEXT, {"okf": OKF_NS}],
                         "results": results, "referrals": [], "pageToken": None,
                         "usage": led.snapshot()})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    release_ok, release_detail = index.verify(require_release=True)
    if not release_ok:
        print("ERROR: registry release is stale or incomplete", file=sys.stderr)
        for error in release_detail.get("errors", [release_detail.get("error", "unknown error")]):
            print(f"  - {error}", file=sys.stderr)
        print(f"Run: {sys.executable} tools/build_registry_release.py", file=sys.stderr)
        raise SystemExit(1)
    print(f"ARD Agent Finder on {SELF} (bind {HOST}:{PORT})  (POST /search) — "
          f"{len(json.load(open(index.CACHE_META)))} tables")
    server = HTTPServer((HOST, PORT), Handler)
    def _stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
