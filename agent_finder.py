#!/usr/bin/env python3
"""ARD Agent Finder — an in-memory registry that serves the OKF data tables.

Implements the ARD discovery contract over HTTP:
  POST /search   {"query": {"text": "..."}, "pageSize": N}
                 -> {"results": [{identifier, displayName, type, source, score}], ...}
  GET  /         service card

The store is the embedded index built by registry/index.py (SEC + Treasury
tables). Run with the Azure keys loaded:
  set -a; source /Users/rvguha/code/test/AskAgent/set_keys.sh; set +a
  python3 agent_finder.py            # serves on http://127.0.0.1:8088
"""
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
from registry import index

PORT = int(os.getenv("AGENT_FINDER_PORT", "8088"))
SELF = f"http://127.0.0.1:{PORT}/"


def publisher(identifier):
    # sources/<source-dir>/<table>.md  ->  the source directory
    parts = identifier.split("/")
    return parts[1] if len(parts) > 2 else "root"


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/"):
            return self._json(200, {"name": "ARD Agent Finder — finance data tables",
                                    "store": "in-memory", "endpoints": {"search": "POST /search"}})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/search":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        text = (req.get("query") or {}).get("text", "")
        k = int(req.get("pageSize", 10))
        results = [{
            "identifier": h["identifier"],
            "displayName": h["title"],
            "type": "application/okf-table+markdown",
            "publisher": publisher(h["identifier"]),
            "source": SELF,
            "score": h["score"],
        } for h in index.search(text, k, sources=req.get("sources"),
                            rerank=req.get("rerank", True))]
        self._json(200, {"results": results, "referrals": [], "pageToken": None})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"ARD Agent Finder on {SELF}  (POST /search) — {len(json.load(open(index.CACHE_META)))} tables")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
