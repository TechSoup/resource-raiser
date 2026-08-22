#!/usr/bin/env python3
"""Generate one OKF leaf per US Census ACS **Data Profile** variable — Census's own
curated set of the most commonly-used statistics (DP02 Social, DP03 Economic,
DP04 Housing, DP05 Demographic), including rate/percent variables. Capped at 500,
balanced across the four profiles. The variable code is pinned; geography is the
query param. No hardwiring beyond the canonical variables metadata.
"""
import os, glob, json, urllib.request
from collections import defaultdict
import yaml

OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "sources", "census"))
VARS_URL = "https://api.census.gov/data/2022/acs/acs5/profile/variables.json"
CAP = 500


def clean_label(label):
    return label.replace("Estimate!!", "").replace("Percent!!", "% ").replace("!!", " — ").rstrip(":").strip()


# the most-queried community indicators — selected by LABEL so they're always in (code-position-proof)
KEY = [
    "median household income (dollars)", "mean household income (dollars)", "per capita income (dollars)",
    "below the poverty level", "unemployment rate", "median age (years)", "total population",
    "bachelor's degree or higher", "high school graduate or higher",
    "with health insurance coverage", "no health insurance coverage",
    "median value (dollars)", "median gross rent (dollars)", "median selected monthly owner costs",
    "owner-occupied", "renter-occupied", "family households",
    "hispanic or latino (of any race)", "white alone", "black or african american alone",
    "asian alone", "two or more races", "foreign born", "veterans", "with a disability",
    "language other than english spoken at home",
]


def main():
    variables = json.load(urllib.request.urlopen(VARS_URL, timeout=180))["variables"]
    allv = [(c, v) for c, v in variables.items()
            if c.endswith("E") and v.get("label") and v.get("concept")]
    must = [(c, v) for c, v in allv if any(k in v["label"].lower() for k in KEY)]

    selected = list(dict(must).items())[:CAP]               # flagship indicators first
    seen = {c for c, _ in selected}
    rest = defaultdict(list)
    for c, v in allv:
        if c not in seen:
            rest[c.split("_")[0]].append((c, v))
    per = max(1, (CAP - len(selected)) // max(1, len(rest)))  # fill the rest, balanced across profiles
    for prefix in sorted(rest):
        g = sorted(rest[prefix])
        step = max(1, len(g) / per)
        selected += [g[int(i * step)] for i in range(min(per, len(g)))]
    selected = selected[:CAP]

    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(os.path.join(OUT, "*.md")):
        if os.path.basename(f) != "_access.md":
            os.remove(f)

    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import repr_queries, descriptions
    byc = dict(selected)

    # The formulaic one-liner below ("ACS variable DP03_0128E: concept (label)") says nothing about
    # what the variable measures or which geographies it is reported for, so near-identical ACS
    # labels are indistinguishable at discovery. Expand each into a full description.
    scope = descriptions.scope_for("census")
    detail = descriptions.for_items(
        [(f"census:{c}", clean_label(v["label"]),
          f"ACS 5-year Data Profile variable {c}: {v.get('concept')} ({clean_label(v['label'])})")
         for c, v in selected],
        "US Census ACS Data Profile variable", scope)

    def write_leaf(code, queries):                            # called per variable as its queries land
        v = byc[code]
        clean = clean_label(v["label"])
        fm = {
            "type": "Census Variable",
            "title": f"{clean} — US Census ACS",
            "description": (detail.get(f"census:{code}")
                            or f"ACS 5-year Data Profile variable {code}: {v.get('concept')} ({clean})."),
            "tags": ["census", "acs", "demographics", "community", "needs-assessment"],
            "source": "./_access.md",
            "get": f"NAME,{code}",
            "key": "env:CENSUS_API_KEY",
            "variable": code,
            "representativeQueries": queries,
        }
        body = (f"# Schema\n\nACS 5-year Data Profile variable `{code}` — {clean}. Returns the value "
                f"for the requested geography (`geo`); `get`/`key` pinned. See "
                f"[Census access](./_access.md).\n")
        with open(os.path.join(OUT, code.lower().replace("_", "-") + ".md"), "w", encoding="utf-8") as fh:
            fh.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body)

    print(f"generating queries + writing {len(selected)} variable leaves incrementally…")
    repr_queries.for_items(
        [(c, clean_label(v["label"]), f"{v.get('concept')} — {clean_label(v['label'])}") for c, v in selected],
        "US Census ACS Data Profile variable", on_ready=write_leaf)
    print(f"wrote {len(glob.glob(os.path.join(OUT, '*.md'))) - 1} census Data Profile variable entries to {OUT}")


if __name__ == "__main__":
    main()
