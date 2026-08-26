#!/usr/bin/env python3
"""Query planner: decide HOW a question can be answered, before fetching anything.

A query implies a set of REQUIRED ACCESS PATHS (indices). Each source's query API
already declares the access paths it PROVIDES — so we DERIVE them from the API
(derive.py: key / filter / order / paginate / enumerate) rather than hand-maintaining a
parallel `capability:` block that can drift. The planner matches required vs. provided:

  exact       one call answers it
  compose:<p> several calls + client-side computation answer it
  infeasible  this source cannot answer this shape — refuse WITHOUT fetching

Two things the query grammar CAN'T show stay declared in the OKF (the residue):
  - grain: what one row represents (county vs project) — a RESPONSE fact, and the gate
    that keeps NIH (orders projects) from being treated as able to rank organizations.
  - empirical limits/quirks (offset ceilings) — handled later.
"""
import os
import driver
import derive

SHAPES = ("point", "status", "entity-list", "comparison", "timeseries",
          "ranking", "aggregate", "filtered-subset", "ratio", "topical", "correlation")

# Non-population shapes: any keyed operation serves them (addressing is handled downstream), so the
# verdict is unconditional. The plan string names how many calls it takes.
_NONPOP = {"point": "exact", "status": "exact", "entity-list": "exact", "topical": "exact",
           "comparison": "compose:fan-out-entities", "timeseries": "compose:fan-out-periods",
           "ratio": "compose:derive"}

# Grains at which one row IS an answer entity. Ordering/enumerating at these grains can rank a
# population; a sub-entity grain (project, award, filing) cannot — that is why NIH, which orders
# PROJECTS, cannot rank ORGANIZATIONS even though its API exposes a sort parameter.
_ENTITY_GRAINS = {"county", "place", "state", "recipient", "organization", "org", "nonprofit", "company"}

IMPLEMENTED = {"exact", "compose:fan-out-entities", "compose:fan-out-periods",
               "compose:scan-and-rank", "compose:scan-and-filter", "compose:generate-and-test",
               "compose:derive", "compose:materialize-and-correlate"}


def access_path(identifier):
    """The `_access.md` a leaf entry cross-links to."""
    fm = driver.frontmatter(identifier) or {}
    src = fm.get("source")
    if not src:
        return None
    return os.path.normpath(os.path.join(os.path.dirname(identifier), src))


def capabilities(identifier):
    """{operation: cap} for this leaf's source. Each cap is the DERIVED access paths (key kind,
    order, enumerate — from derive.py, cached) MERGED with the OKF's declared RESIDUE (grain,
    returns, page limits, completeness) — the facts the query grammar can't show."""
    try:
        fm = driver.frontmatter(identifier) or {}
        if fm.get("access"):
            acc = fm["access"]
        else:
            p = access_path(identifier)
            acc = (driver.frontmatter(p) or {}).get("access") if p else {}
    except Exception:
        return {}
    out = {}
    for op, spec in (acc.get("operations") or {}).items():
        residue = spec.get("capability") or {}            # grain/returns/page… and, for SQL, declared paths
        # CREDENTIAL GATE: a source that requires an env var (BigQuery -> a GCP project) is invisible
        # to the planner until it is set, so its shapes fall back to the honest refusal — no regression.
        req = residue.get("requires_env")
        if req and not os.getenv(req):
            continue
        d = derive.capability(spec)                       # derived access paths from the query grammar
        merged = {**residue, **d}
        # A SQL source has no URL grammar to derive from, so it DECLARES its paths; union them in.
        if residue.get("paths"):
            merged["paths"] = sorted(set(residue["paths"]) | set(d.get("paths") or []))
        merged["order"] = {**(d.get("order") or {}), **(residue.get("order") or {})}
        merged["population"] = {**(residue.get("population") or {}), **(d.get("population") or {})}
        out[op] = merged
    return out


def _population_scan(cap):
    """How this operation can see the WHOLE population AT ENTITY GRAIN, or None.

    'order'     -> the server returns entities already ordered by the measure (top-N is one call).
    'enumerate' -> the whole population can be listed (rank/aggregate locally).
    Requires an ENTITY grain: an ordered scan over sub-entity rows (NIH projects) cannot rank the
    entities. `population.complete: false` (a declared ceiling) also disqualifies enumeration."""
    if cap.get("grain") not in _ENTITY_GRAINS:
        return None
    paths = set(cap.get("paths") or [])
    if "order" in paths:
        return "order"
    pop = cap.get("population") or {}
    if "enumerate" in paths and pop.get("complete", True) is not False:
        return "enumerate"
    return None


def verdict(shape, identifier):
    """Best verdict this source can offer for this shape. Returns (verdict, operation, cap, why)."""
    caps = capabilities(identifier)
    if shape in _NONPOP:                                  # any keyed op serves it (or a bare keyed read)
        if not caps:
            return (_NONPOP[shape], None, {}, "no access block; assuming keyed read")
        op, cap = next(iter(caps.items()))
        return (_NONPOP[shape], op, cap, "")
    if not caps:                                          # population shape with no usable op (e.g. a
        return ("infeasible", None, {},                  # credential-gated source that isn't configured)
                "no operation exposes an entity-grain population scan")
    # population shapes (ranking / aggregate / filtered-subset / correlation): need a population scan
    best = None
    for op, cap in caps.items():
        scan = _population_scan(cap)
        if not scan:
            continue
        if shape == "correlation":
            v = "compose:materialize-and-correlate"
        elif shape == "filtered-subset":
            v = "compose:scan-and-filter"
        elif shape in ("ranking", "aggregate"):
            v = "exact" if scan == "order" else "compose:scan-and-rank"
        else:
            v = "exact"
        rank = 0 if v == "exact" else 1
        if best is None or rank < best[0]:
            best = (rank, v, op, cap)
    if best is None:
        paths = sorted({p for c in caps.values() for p in (c.get("paths") or [])})
        return ("infeasible", None, {},
                f"no operation exposes an entity-grain population scan (offers {', '.join(paths)})")
    return (best[1], best[2], best[3], "")


def plan(shape, hits, quantifier="exhaustive"):
    """Pick the best (verdict, hit) across candidate sources for this shape.

    Refusal happens HERE — before any HTTP request — because 'no point even trying' is a
    planning decision, not a failed fetch."""
    scored, reasons, rejected = [], [], []
    for position, h in enumerate(hits):
        v, op, cap, why = verdict(shape, h["identifier"])
        # EXISTENTIAL ("give me SOME X over N") asks only for examples, not for membership of the
        # whole set — a far weaker bar than the exhaustive form. It needs no population capability:
        # propose candidates, then VERIFY each against a keyed read. Only valid where the source can
        # produce a COMPLETE per-entity value, or every verdict would be a truncated false negative.
        if v == "infeasible" and shape == "filtered-subset" and quantifier == "existential":
            for _op, _cap in capabilities(h["identifier"]).items():
                page, paths = (_cap.get("page") or {}), set(_cap.get("paths") or [])
                key = (_cap.get("key") or {}).get("kind")
                # per-entity value must be COMPLETE: a pure keyed point read, or a keyed list paged fully
                per_entity_complete = page.get("complete_for") == "entity" or paths == {"key"}
                if key in ("canonical-id", "name") and per_entity_complete:
                    v, op, cap, why = "compose:generate-and-test", _op, _cap, ""
                    break
        if v == "infeasible":
            reason = f"{h.get('publisher') or h['identifier']} {why}"
            reasons.append(reason)
            rejected.append({"identifier": h["identifier"], "title": h.get("title", ""),
                             "publisher": h.get("publisher"), "reason": why,
                             "outcome": "structurally-infeasible"})
            continue
        if v not in IMPLEMENTED:
            reason = f"needs {v} (not implemented)"
            reasons.append(f"{h.get('publisher') or h['identifier']} {reason}")
            rejected.append({"identifier": h["identifier"], "title": h.get("title", ""),
                             "publisher": h.get("publisher"), "reason": reason,
                             "outcome": "not-implemented"})
            continue
        # Lexicographic fitness, not arbitrary weights: direct execution first, then complete
        # population coverage, then the semantic order supplied by discovery.
        population = cap.get("population") or {}
        completeness = 0 if population.get("complete", True) is not False else 1
        scored.append((0 if v == "exact" else 1, completeness, position, h, v, op, cap))
    if not scored:
        uniq = list(dict.fromkeys(reasons))[:3]
        return {"verdict": "infeasible", "why": "; ".join(uniq) or "no candidate source",
                "rejected": rejected}
    scored.sort(key=lambda s: s[:3])
    _, _, _, hit, v, op, cap = scored[0]
    return {"verdict": v, "hit": hit, "operation": op, "capability": cap,
            "alternatives": [s[3] for s in scored[1:]], "rejected": rejected}


def describe(shape, p):
    """One-line human summary of the chosen plan, for the streamed console."""
    if p["verdict"] == "infeasible":
        return f"{shape}: no source can answer this — {p['why']}"
    if p["verdict"] == "exact":
        return f"{shape}: one direct query against {p['hit']['title']}"
    return f"{shape}: {p['verdict'].split(':', 1)[1]} using {p['hit']['title']}"
