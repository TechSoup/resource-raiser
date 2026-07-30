#!/usr/bin/env python3
"""Thin ARD client — calls a remote Agent Finder's POST /search.

This is the discovery seam: everything that needs to find a table goes through
here, so the registry can be a separate process (or remote) rather than an
in-process call. Configure the finder with AGENT_FINDER_URL.
"""
import os, json, socket, urllib.request, urllib.error

BASE = os.getenv("AGENT_FINDER_URL", "http://127.0.0.1:8088").rstrip("/")
# Generous default: a rerank on a slow LOCAL model can take minutes; a too-short timeout was raising
# TimeoutError (a subclass of neither URLError nor ConnectionError), which escaped to the top -> HTTP 000.
TIMEOUT = int(os.getenv("AGENT_FINDER_TIMEOUT", "180"))


def search(text, k=10, sources=None, rerank=True):
    body = json.dumps({"query": {"text": text}, "pageSize": k, "sources": sources, "rerank": rerank}).encode()
    req = urllib.request.Request(BASE + "/search", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            results = json.load(r).get("results", [])
    except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, OSError) as e:
        raise SystemExit(f"agent finder unreachable or too slow at {BASE} ({e}). "
                         f"Start it (python3 agent_finder.py); for slow local models raise "
                         f"AGENT_FINDER_TIMEOUT or set ARD_RERANK=0.")
    return [{"identifier": x["identifier"], "title": x.get("displayName", ""),
             "score": x.get("score"), "publisher": x.get("publisher")} for x in results]
