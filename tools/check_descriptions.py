#!/usr/bin/env python3
"""Verify generated descriptions against the definitions they were expanded from.

A description is a discovery aid, but it is also read by the re-ranker and shown in the UI, so a
confidently wrong one is worse than a thin one. The expansion prompt forbids inventing facts;
this checks whether it obeyed.

Ground truth is git: every leaf's committed description is the definition the expansion started
from, so `git show HEAD:<path>` gives the exact "before" for each "after" — no separate record to
keep in sync.

Two stages, because LLM-judging thousands of leaves is slow and most are fine:

  1. SCREEN (free, deterministic) — flag the failure classes an expansion actually commits:
     sharpened hedges (source says "mostly", description says "only"), invented numbers,
     invented form/section citations, and scope restrictions absent from the source.
  2. ADJUDICATE (LLM) — only the screened subset is judged, each against its own source text,
     and asked the single question that matters: does the description assert something the
     source does not support?

    python3 tools/check_descriptions.py                  # screen only, summary + samples
    python3 tools/check_descriptions.py --adjudicate     # + LLM pass over the screened subset
    python3 tools/check_descriptions.py --adjudicate --json report.json
"""
import os, re, sys, json, glob, argparse, subprocess, collections
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
import descriptions

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SOURCES = os.path.join(ROOT, "sources")
INPUTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "descriptions_input.json")


def baseline_key(src, fm, path):
    """The descriptions-cache key for a leaf — how the side-car of expansion inputs is addressed.

    Each generator keys its items by the source's own identifier (a us-gaap concept, an ACS
    variable code, a 990 field), so the mapping is per-source. Hand-authored leaves, which
    tools/enrich_descriptions.py handles, are keyed by filename."""
    if src == "census" and fm.get("variable"):
        dataset = fm.get("dataset") or "profile"
        return (f"census:{fm['variable']}" if dataset == "profile"
                else f"census:{dataset}:{fm['variable']}")
    if src == "cdc-places" and fm.get("measureid"):
        return f"cdc-places:{fm['measureid']}"
    if src == "nonprofit-990" and fm.get("field"):
        return f"nonprofit-990:{fm['field']}"
    if src == "sec-edgar" and fm.get("concept"):
        return f"sec-edgar:{fm['concept']}"
    if src == "treasury" and fm.get("path"):
        if fm.get("filter"):
            return f"treasury:{fm['path']}|{fm['filter'].split(':eq:')[-1]}"
        return f"treasury:{fm['path']}|{fm.get('tfield')}"
    return f"{src}:{os.path.basename(path)}"

HEDGE = re.compile(r"\b(mostly|typically|largely|generally|usually|often|may|can|some)\b", re.I)
ABSOLUTE = re.compile(r"\b(only|exclusively|specific to|restricted to|limited to|solely|"
                      r"always|never|must be|all US|every)\b", re.I)
CITATION = re.compile(r"\b(part [ivxlc\d]+|line \d+|schedule [a-z]\b|section \d|§|"
                      r"irc\b|form \d{3,4}\b|table [a-z]?\d)", re.I)
NUMBER = re.compile(r"\b\d[\d,.]*\b")
# Numbers that are part of the concept's own vocabulary, not invented facts.
BENIGN_NUM = re.compile(r"\b(501\(c\)|990|10-K|10-Q|509\(a\)|170\(b\)|4-year|5-year|18-64|12 months|"
                        r"one|two|three|1099|W-2)\b", re.I)


def fm_of(text):
    if not text or not text.startswith("---"):
        return {}
    try:
        return yaml.safe_load(text.split("---", 2)[1]) or {}
    except Exception:
        return {}


def committed(relpath):
    try:
        return subprocess.run(["git", "show", f"HEAD:{relpath}"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return ""


def screen(old_desc, new_desc, scope, label):
    """Deterministic suspicion flags. Cheap, over-inclusive by design — the LLM pass decides."""
    flags = []
    src = f"{old_desc} {label} {scope}"
    if ABSOLUTE.search(new_desc) and not ABSOLUTE.search(src):
        flags.append("absolute-not-in-source")
    if HEDGE.search(scope) and ABSOLUTE.search(new_desc):
        flags.append("sharpened-hedge")
    if CITATION.search(new_desc) and not CITATION.search(src):
        flags.append("invented-citation")
    old_nums = set(NUMBER.findall(src))
    new_nums = {n for n in NUMBER.findall(new_desc)
                if n not in old_nums and not BENIGN_NUM.search(n)}
    if new_nums:
        flags.append(f"invented-number:{','.join(sorted(new_nums)[:3])}")
    return flags


JUDGE = (
    "You verify a data-catalog description against the source definition it was expanded from.\n"
    "For each numbered item you get the SOURCE definition, the subject SCOPE, and the written "
    "DESCRIPTION.\n\n"
    "Answer ONE question per item: does the DESCRIPTION assert something the SOURCE and SCOPE do "
    "not support? Count as unsupported:\n"
    "  - a scope restriction the source does not state ('only nonprofits', 'exclusively public "
    "companies') — including hardening a hedge like 'mostly' into 'only';\n"
    "  - an invented number, threshold, date, form line, schedule, or statutory citation;\n"
    "  - a claim that contradicts the source definition.\n"
    "Do NOT flag: restating the source in other words, adding the subject scope as given, naming "
    "an obvious unit or reporting grain, or explaining how the measure differs from a sibling "
    "measure it names.\n"
    'Return JSON {"items":[{"i":<number>,"unsupported":true|false,"why":"<short, or empty>"}]}.'
)


def adjudicate(items, batch=8, workers=None):
    """items: list of dicts with source/scope/description. Returns {index: (unsupported, why)}.

    Batches run concurrently — judging a few thousand screened leaves one round trip at a time
    takes long enough that the check gets skipped, and a check that gets skipped is no check."""
    import driver
    from concurrent.futures import ThreadPoolExecutor
    workers = workers or int(os.getenv("DESCRIPTIONS_WORKERS", "8"))
    chunks = [(start, items[start:start + batch]) for start in range(0, len(items), batch)]

    def judge(job):
        start, chunk = job
        listing = "\n\n".join(
            f"{j}.\nSOURCE: {(it['old'] or it['label'])[:400]}\nSCOPE: {it['scope'][:200]}\n"
            f"DESCRIPTION: {it['new'][:700]}" for j, it in enumerate(chunk))
        try:
            res = json.loads(driver.ask_llm(JUDGE, listing, json_mode=True)).get("items", [])
        except Exception as e:
            print(f"  ! judge batch failed ({type(e).__name__}: {str(e)[:80]}) — {len(chunk)} unjudged")
            res = []
        out = {}
        for r in res:
            j = r.get("i")
            if isinstance(j, int) and 0 <= j < len(chunk):
                out[start + j] = (bool(r.get("unsupported")), (r.get("why") or "").strip())
        return out

    verdicts, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for out in pool.map(judge, chunks):
            verdicts.update(out)
            done += batch
            print(f"  adjudicated {min(done, len(items))}/{len(items)}")
    return verdicts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--adjudicate", action="store_true", help="LLM-judge the screened subset")
    ap.add_argument("--sources", default="", help="comma-separated source dirs (default: all)")
    ap.add_argument("--json", default="", help="write the full report here")
    ap.add_argument("--limit", type=int, default=0, help="adjudicate at most N screened leaves")
    a = ap.parse_args(argv)
    want = {s.strip() for s in a.sources.split(",") if s.strip()}

    inputs = json.load(open(INPUTS)) if os.path.exists(INPUTS) else {}
    if not inputs:
        print("WARNING: no tools/descriptions_input.json — re-run the generators so the expansion "
              "inputs are recorded; without them there is nothing to verify against.")
    scopes, checked, screened, no_baseline = {}, 0, [], 0
    for path in sorted(glob.glob(os.path.join(SOURCES, "**", "*.md"), recursive=True)):
        if os.path.basename(path) == "_access.md":
            continue
        rel = os.path.relpath(path, ROOT)
        src = os.path.relpath(path, SOURCES).split(os.sep)[0]
        if want and src not in want:
            continue
        fm = fm_of(open(path, encoding="utf-8").read())
        if not fm.get("representativeQueries"):
            continue
        new = (fm.get("description") or "").strip()
        # Prefer the recorded expansion input; most generated leaves are gitignored, so the
        # committed file is usually absent and would silently become an empty baseline.
        old = (inputs.get(baseline_key(src, fm, path)) or "").strip()
        if not old:
            old = (fm_of(committed(rel)).get("description") or "").strip()
        if not new or new == old:
            continue                                   # unchanged leaves need no verification
        if not old:
            no_baseline += 1                           # nothing to check against — do NOT judge it
            continue
        checked += 1
        if src not in scopes:
            scopes[src] = descriptions.scope_for(src)
        flags = screen(old, new, scopes[src], fm.get("title", ""))
        if flags:
            screened.append({"path": rel, "source": src, "label": fm.get("title", ""),
                             "old": old, "new": new, "scope": scopes[src], "flags": flags})

    print(f"checked {checked} rewritten descriptions against their expansion input; "
          f"{len(screened)} screened as suspicious ({100.0 * len(screened) / max(1, checked):.1f}%)")
    if no_baseline:
        print(f"skipped {no_baseline} leaves with no recorded baseline (not verifiable)")
    by_flag = collections.Counter(f.split(":")[0] for s in screened for f in s["flags"])
    for f, n in by_flag.most_common():
        print(f"  {f:26} {n:>5}")

    report = {"checked": checked, "screened": len(screened), "by_flag": dict(by_flag), "findings": []}
    if a.adjudicate and screened:
        subset = screened[:a.limit] if a.limit else screened
        print(f"\nadjudicating {len(subset)} screened leaves…")
        verdicts = adjudicate(subset)
        bad = [{**subset[i], "why": why} for i, (uns, why) in sorted(verdicts.items()) if uns]
        report["adjudicated"] = len(subset)
        report["findings"] = [{k: v for k, v in b.items() if k != "scope"} for b in bad]
        print(f"\n{len(bad)} of {len(subset)} confirmed unsupported "
              f"({100.0 * len(bad) / max(1, len(subset)):.1f}% of screened, "
              f"{100.0 * len(bad) / max(1, checked):.2f}% of all rewritten)")
        for b in bad[:15]:
            print(f"\n  {b['path']}\n    flags: {', '.join(b['flags'])}\n    why: {b['why']}"
                  f"\n    said: {b['new'][:180]}")
    elif screened:
        for s in screened[:10]:
            print(f"\n  {s['path']}\n    flags: {', '.join(s['flags'])}\n    said: {s['new'][:180]}")

    if a.json:
        json.dump(report, open(a.json, "w"), indent=1)
        print(f"\nreport -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
