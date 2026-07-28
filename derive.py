#!/usr/bin/env python3
"""Derive a source's ACCESS PATHS from the query API it already describes in OKF.

The `access` block already carries the API's query interface — the URL template, the
parameters, the request body, the query dialect. What a source can DO (look up by key,
order, filter, enumerate a population) is a function of that interface, so it should be
DERIVED, not declared a second time in a parallel `capability:` block that can drift.

Access paths (the "indices" a source offers):
    key        fetch one record by its primary key
    filter     restrict by a non-key predicate
    order      server-side ordering by a measure
    paginate   walk the result set in pages
    enumerate  scan/enumerate the whole population

Two things are NOT visible in the query grammar and stay declared (the residue):
    - hard limits the API won't self-describe (NIH's ~15k offset ceiling)
    - behavioral quirks (NSF's %22, ACS jam values) — prose, handled later
Everything else here is read off the API.
"""
import re

# A named query dialect fixes the whole path set — recognizing it is enough.
_DIALECTS = [
    ("socrata", lambda u: "data.cdc.gov" in u or "/resource/" in u,
     {"key", "filter", "order", "aggregate", "enumerate"}),          # SoQL: $where/$order/$group/$limit
    ("census-geo", lambda u: "api.census.gov" in u,
     {"key", "enumerate"}),                                          # geography grammar: for=<lvl>:* , in=
    ("json-api", lambda u: "fiscaldata.treasury.gov" in u,
     {"key", "filter", "order", "paginate", "enumerate"}),           # filter= sort= page[]
]

_KEY_ID = ("cik", "ein", "qid", "gnis", "lei", "geo")               # canonical identifiers
_KEY_NAME = ("q", "org", "awardee", "awardeename", "name", "org_names",
             "recipient_search_text", "place")                       # fuzzy name selectors
_KEY_KW = ("keyword",)


def _names(op):
    url, body = op.get("url", ""), op.get("body", "") or ""
    ph = set(re.findall(r"\{(\w+)\}", url))                          # {cik}, {geo}, {n}
    ph |= set(re.findall(r"\$(\w+)", body))                         # $org, $offset
    ph |= set(re.findall(r"[?&]([A-Za-z_]+)=", url))                # ?awardeeName=, &sort=
    ph |= set(re.findall(r'"(\w+)"\s*:', body))                    # body json keys
    return {n.lower() for n in ph}


def _key(names):
    for n in names:
        if n in _KEY_ID or n.startswith("fips"):
            return {"field": n, "kind": "canonical-id"}
    for n in names:
        if n in _KEY_NAME:
            return {"field": n, "kind": "name"}
    for n in names:
        if n in _KEY_KW:
            return {"field": n, "kind": "keyword"}
    return {"field": None, "kind": "none"}


_CACHE = {}


def access_paths(op):
    """Return {'paths': set, 'key': {...}, 'dialect': str} derived from the operation. Cached:
    the access block is static, so an operation's paths are derived once per process."""
    ck = op.get("url", "") + "|" + (op.get("body") or "")
    if ck in _CACHE:
        return _CACHE[ck]
    _CACHE[ck] = _access_paths(op)
    return _CACHE[ck]


def _access_paths(op):
    url = op.get("url", "")
    names = _names(op)
    for name, match, paths in _DIALECTS:
        if match(url):
            return {"dialect": name, "paths": set(paths), "key": _key(names)}
    text = (url + " " + (op.get("body") or "")).lower()
    paths = {"key"}
    if re.search(r"\bsort\b|\border\b|sort_field|\$order", text):
        paths.add("order")
    if re.search(r"\bfilter\b|\$where|criteria|search_text|[?&]q=", text):
        paths.add("filter")
    if re.search(r"offset|page\[|\$limit|\blimit\b|pagesize", text):
        paths.add("paginate")
    if ":*" in text:                                               # wildcard selector
        paths.add("enumerate")
    return {"dialect": "rest", "paths": paths, "key": _key(names)}


def klass(op):
    """The coarse capability class the matrix wants, derived from the access-path set + key kind.
    (A stopgap while the matrix still keys on a class enum; the paths set is the real currency.)"""
    ap = access_paths(op)
    p, kind = ap["paths"], ap["key"]["kind"]
    if "order" in p and ("aggregate" in p or "enumerate" in p):
        return "server-aggregate"
    if "enumerate" in p:
        return "population-scan"
    if kind in ("name", "keyword"):
        return "predicate-search"
    return "point"


def capability(op):
    """Access-path facts in the shape the planner already consumes — DERIVED from the API.
    Response/cost/empirical fields (grain, returns, ceiling, rows_per_unit) are NOT here; those
    stay in the OKF because they are not visible in the query grammar."""
    ap = access_paths(op)
    p = ap["paths"]
    return {
        "class": klass(op),
        "key": ap["key"],
        "paths": sorted(p),
        "order": {"server": "order" in p},
        # enumerable is derivable; COMPLETENESS is empirical (a ceiling the grammar doesn't show),
        # so the planner still reads population.complete from the OKF where it matters.
        "population": {"enumerable": "enumerate" in p},
        "dialect": ap["dialect"],
    }
