#!/usr/bin/env python3
"""Measure DISCOVERY ROUTING alone — did the question reach the right source?

run_queries.py exercises the whole pipeline, so a failure there can be a missing API key, a
flaky endpoint, or a fetch quirk. This isolates the one step that description quality governs:
given a question, does the ARD index surface a table from the source that can actually answer it?

The corpus already carries the answer: cases tagged with `dirs` name the source directories a
question belongs to. This replays those cases through the real discovery path — classify, then
search on the entity-expunged attribute, exactly as harness.discover does — and reports how often
the top hit, and the top 3, land in an expected directory.

Needs the agent finder up (AGENT_FINDER_URL) and one provider's keys. No data-source keys: it
never fetches, so a missing Census key cannot skew the result.

    python3 tests/route_eval.py                     # every tagged case
    python3 tests/route_eval.py --limit 60          # a quick slice
    python3 tests/route_eval.py --json before.json  # save, to diff against a later run
"""
import os, sys, json, argparse, collections, hashlib, subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..")))
import harness                                                # the real discover() path
import ard_client


def evaluate(case):
    q = case["q"]
    tally = ard_client.start_usage()          # what discovery cost for THIS case
    try:
        ctx, hits = harness.discover(q)
    except Exception as e:
        return {"q": q, "error": str(e)[:120], "top": None, "hit1": False, "hit3": False,
                "discovery": tally.snapshot()}
    pubs = [h.get("publisher") for h in hits]
    want = set(case["dirs"])
    return {"q": q, "want": sorted(want),
            "intent": {key: ctx.get(key) for key in ("type", "attribute", "shape", "sources")},
            "top": pubs[0] if pubs else None,
            "top3": pubs[:3], "hit1": bool(pubs and pubs[0] in want),
            "hit3": any(p in want for p in pubs[:3]), "error": None,
            "discovery": tally.snapshot()}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default="")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)

    cases = [c for c in json.load(open(os.path.join(HERE, "queries.json")))["cases"] if c.get("dirs")]
    if a.limit:
        cases = cases[:a.limit]
    print(f"routing {len(cases)} tagged cases…")

    # discover() writes progress through a thread-local emitter, so parallel calls stay independent.
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        rows = list(pool.map(evaluate, cases))

    ok1 = sum(r["hit1"] for r in rows)
    ok3 = sum(r["hit3"] for r in rows)
    err = sum(bool(r["error"]) for r in rows)
    n = len(rows)
    cost = sum((r.get("discovery") or {}).get("cost_usd", 0.0) for r in rows)
    toks = sum((r.get("discovery") or {}).get("total_tokens", 0) for r in rows)
    print(f"\ntop-1 correct source: {ok1}/{n} ({100.0 * ok1 / n:.1f}%)")
    print(f"top-3 correct source: {ok3}/{n} ({100.0 * ok3 / n:.1f}%)")
    print(f"discovery cost      : ${cost:.4f} total, ${cost / n:.5f}/question, "
          f"{toks / n:,.0f} tokens/question")
    if err:
        print(f"errors: {err}")

    miss = collections.Counter()
    for r in rows:
        if not r["hit1"] and not r["error"]:
            miss[f"{'/'.join(r['want'])} -> {r['top']}"] += 1
    if miss:
        print("\nmisroutes (expected -> got):")
        for k, v in miss.most_common(12):
            print(f"  {v:>3}  {k}")

    if a.json:
        root = os.path.normpath(os.path.join(HERE, ".."))
        def digest(name):
            with open(os.path.join(root, "tools", name), "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                    capture_output=True, text=True).stdout.strip()
        except Exception:
            commit = "unknown"
        report = {
            "manifest": {"created_at": datetime.now(timezone.utc).isoformat(),
                         "command": " ".join([sys.executable, __file__, *(argv or sys.argv[1:])]),
                         "commit": commit, "provider": __import__("llm").provider(),
                         "chat_model": __import__("llm").chat_model(),
                         "embedding_model": __import__("llm").embed_model(),
                         "live": True,
                         "prompt_versions": {n: digest(n) for n in ("descriptions.py", "repr_queries.py")}},
            "n": n, "top1": ok1, "top3": ok3, "discovery_cost_usd": round(cost, 5),
            "rows": rows,
        }
        with open(a.json, "w") as f:
            json.dump(report, f, indent=1)
        print(f"\nsaved -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
