#!/usr/bin/env python3
"""Materialization + cache layer — an incrementally built, demand-driven data commons.

Entries are normalized OBSERVATIONS, not per-source blobs:

    (entity, measure, period) -> value   + unit + source + provenance

which is deliberately the Data Commons shape: `entity` is a canonical id on the
resolver's spine (`fips/06001`, `qid/Q180`, `ein/530196605`), and one OKF leaf plays
the role of one StatVar. Storing it this way means the cross-source join is paid ONCE,
at materialization, on the spine — after which any question combining those measures is
a local lookup, no matter which API originally supplied each one. The commons ACCRETES:
every query that materializes a measure makes every future question touching it cheaper.

WHEN to materialize is a cost decision, and the deciding number is
BLOWUP = rows transferred / units in the answer:

    Census ACS   one row per county   ->     58 rows for  58 counties ->    1x  (free)
    CDC PLACES   one row per county   ->  3,000 rows for 3k counties  ->    1x  (free)
    NIH          one row per project  -> 83,516 rows for 50 states    -> 1670x  (expensive)

1x means the source cannot reduce further anyway, so local costs nothing extra — just do
it. A large blowup is un-pushable residue: reasonable ONCE per vintage (immutable history),
never per question.
"""
import os, re, json, time, hashlib
import store_backends

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
DEFAULT_MAX_AGE_DAYS = 30

# The backend is chosen by DEPLOYMENT (sqlite locally, managed services in cloud), never
# hardcoded. Callers below are backend-agnostic; see store_backends.detect().
_BACKEND, _WHY = store_backends.build(ROOT)


def backend():
    return _BACKEND


def backend_info():
    return {"backend": _BACKEND.name, "detail": _BACKEND.describe(), "why": _WHY}


# --- canonical entity ids (the spine's addressing scheme) ------------------------
def eid(kind, value):
    """A canonical entity id. Same real-world thing -> same id, whichever source supplied it."""
    v = str(value).strip()
    if kind == "fips":
        v = re.sub(r"\D", "", v)
    return f"{kind}/{v}"


def key(measure, grain, vintage="latest"):
    """Cache identity for one measure at one grain and edition."""
    return f"{measure}|{grain}|{vintage}"


def get(k, max_age_days=DEFAULT_MAX_AGE_DAYS):
    e = _BACKEND.read(k)
    if e is None:
        return None
    if max_age_days is not None and (time.time() - e.get("fetched_at", 0)) > max_age_days * 86400:
        return None
    return e


def put(k, observations, meta=None):
    return _BACKEND.write(k, observations, meta)


def ensure(measure, grain, vintage, builder, max_age_days=DEFAULT_MAX_AGE_DAYS, say=None):
    """Observations for one measure at one grain, materializing only on a cache miss.
    `builder()` returns (observations, meta); each observation needs at least entity+value."""
    k = key(measure, grain, vintage)
    hit = get(k, max_age_days)
    if hit is not None:
        if say:
            say(f"cache hit — {hit['n']} observations already materialized ({measure} @ {grain})")
        return hit["observations"], True
    if say:
        say(f"materializing {measure} @ {grain} …")
    obs, meta = builder()
    put(k, obs, meta)
    if say:
        say(f"materialized {len(obs)} observations into the commons ({measure} @ {grain})")
    return obs, False


def align(series):
    """THE LOCAL JOIN. `series` = {label: [observation, ...]}. Returns (rows, report) where each
    row is {entity, entity_name, <label>: value, ...} for entities present in EVERY series.

    Sources need no shared key of their own: they were normalized onto the spine at
    materialization, so alignment here is a dict intersection."""
    if not series:
        return [], {}
    idx, names = {}, {}
    for label, obs in series.items():
        for o in obs:
            e, v = o.get("entity"), o.get("value")
            if e is None or not isinstance(v, (int, float)):
                continue
            idx.setdefault(e, {})[label] = v
            names.setdefault(e, o.get("entity_name") or e)
    labels = list(series)
    rows = [{"entity": e, "entity_name": names[e], **vals}
            for e, vals in idx.items() if all(l in vals for l in labels)]
    rows.sort(key=lambda r: r["entity"])
    report = {"matched": len(rows),
              "per_series": {l: sum(1 for o in series[l] if isinstance(o.get("value"), (int, float)))
                             for l in labels},
              "dropped_unmatched": {l: sum(1 for o in series[l]
                                           if isinstance(o.get("value"), (int, float))
                                           and o.get("entity") not in {r["entity"] for r in rows})
                                    for l in labels}}
    return rows, report


def estimate(cap, unit, n_units):
    """Predicted transfer cost of materializing `n_units` of `unit`, from DECLARED capability.
    The planner compares this to a budget BEFORE fetching, so an expensive materialization is
    a decision rather than a surprise."""
    grain = cap.get("grain")
    rpu = (cap.get("rows_per_unit") or {}).get(unit)
    page = int((cap.get("page") or {}).get("max") or 500)
    if grain == unit or unit in (cap.get("can_aggregate_to") or []):
        rows = n_units
    elif rpu:
        rows = int(n_units * float(rpu))
    else:
        return {"rows": None, "requests": None, "blowup": None, "known": False}
    return {"rows": rows, "requests": max(1, -(-rows // page)),
            "blowup": round(rows / max(1, n_units), 1), "known": True}


def stats():
    return _BACKEND.list()


def clear(prefix=""):
    return _BACKEND.clear(prefix) if hasattr(_BACKEND, "clear") else _BACKEND.delete(prefix)


if __name__ == "__main__":
    import sys
    info = backend_info()
    print(f"backend: {info['backend']} — {info['detail']}\n         ({info['why']})\n")
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        print(f"cleared {clear(sys.argv[2] if len(sys.argv) > 2 else '')} entries")
    else:
        for e in stats():
            print(f"{e['rows']:>8} obs  {e['age_hours']:>7.1f}h  {e['key']}")
        print(f"\n{len(stats())} materialized measures in {CACHE}")
