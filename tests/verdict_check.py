import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import planner

CASES = [
    ("ranking", "sources/cdc-places/diabetes.md", "exact/scan-and-rank"),
    ("ranking", "sources/usaspending/federal-awards-received.md", "exact"),
    ("ranking", "sources/nih-reporter/research-grants.md", "INFEASIBLE (orders projects, not orgs)"),
    ("ranking", "sources/nonprofit-990/totrevenue.md", "INFEASIBLE (key-only)"),
    ("ranking", "sources/sec-edgar/revenues.md", "INFEASIBLE (key-only -> P/E needs index)"),
    ("correlation", "sources/census/dp03-0062e.md", "materialize-and-correlate"),
    ("correlation", "sources/cdc-places/diabetes.md", "materialize-and-correlate"),
    ("filtered-subset", "sources/cdc-places/diabetes.md", "scan-and-filter"),
    ("filtered-subset", "sources/nih-reporter/research-grants.md", "INFEASIBLE"),
    ("comparison", "sources/nih-reporter/research-grants.md", "fan-out-entities"),
    ("point", "sources/nonprofit-990/totrevenue.md", "exact"),
    ("timeseries", "sources/sec-edgar/revenues.md", "fan-out-periods"),
    ("topical", "sources/grants-gov/funding-opportunities.md", "exact"),
]
for shape, ident, expect in CASES:
    if not os.path.exists(ident):
        print(f"  {shape:15} (missing {ident})")
        continue
    v, op, cap, why = planner.verdict(shape, ident)
    src = os.path.basename(os.path.dirname(ident))
    tail = f" — {why}" if why else ""
    print(f"  {shape:15} {src:15} -> {v:32}{tail}\n{'':34}expect: {expect}")
