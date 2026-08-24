#!/usr/bin/env python3
"""Generate one OKF leaf per selected US Census ACS variable.

Include every eligible Data Profile estimate first: Census's curated social,
economic, housing, and demographic statistics. Fill the remaining catalog capacity
with a deterministic, balanced sample of Subject Table estimates. The variable code
and dataset are pinned; geography is the query parameter.
"""
import os, glob, json, urllib.request
from collections import defaultdict
import yaml

OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "sources", "census"))
VARS_URLS = {
    "profile": "https://api.census.gov/data/2022/acs/acs5/profile/variables.json",
    "subject": "https://api.census.gov/data/2022/acs/acs5/subject/variables.json",
}
CAP = 2000


def clean_label(label):
    return label.replace("Estimate!!", "").replace("Percent!!", "% ").replace("!!", " — ").rstrip(":").strip()


def _eligible(variables):
    return sorted((c, v) for c, v in variables.items()
                  if c.endswith("E") and v.get("label") and v.get("concept"))


def _balanced(rows, limit):
    """Take a stable cross-section across ACS table prefixes, retaining each table's first row."""
    groups = defaultdict(list)
    for code, value in rows:
        groups[code.split("_")[0]].append((code, value))
    names = sorted(groups)
    if not names or limit <= 0:
        return []
    base, extra = divmod(limit, len(names))
    picked = []
    for pos, name in enumerate(names):
        group = sorted(groups[name])
        quota = min(len(group), base + (pos < extra))
        if quota == 1:
            indices = [0]
        elif quota > 1:
            indices = [round(i * (len(group) - 1) / (quota - 1)) for i in range(quota)]
        else:
            indices = []
        picked.extend(group[i] for i in dict.fromkeys(indices))
    # Tiny groups can leave unused capacity; fill it deterministically from unselected rows.
    seen = {code for code, _ in picked}
    if len(picked) < limit:
        picked.extend((code, value) for code, value in rows if code not in seen)
    return picked[:limit]


def select_variables(by_dataset, cap=CAP):
    """Return `(dataset, code, metadata)` entries, Data Profiles first then Subject Tables."""
    profiles = _eligible(by_dataset.get("profile", {}))
    selected = [("profile", code, value) for code, value in profiles[:cap]]
    remaining = cap - len(selected)
    if remaining > 0:
        subjects = _balanced(_eligible(by_dataset.get("subject", {})), remaining)
        selected.extend(("subject", code, value) for code, value in subjects)
    return selected


def _item_key(dataset, code):
    return code if dataset == "profile" else f"{dataset}:{code}"


def _description_key(dataset, code):
    # Preserve the original Data Profile cache keys so expanding 500 -> 2,000 regenerates only
    # genuinely new descriptions. Subject codes live in their own namespace.
    return f"census:{code}" if dataset == "profile" else f"census:{dataset}:{code}"


def main():
    by_dataset = {name: json.load(urllib.request.urlopen(url, timeout=180))["variables"]
                  for name, url in VARS_URLS.items()}
    selected = select_variables(by_dataset)
    if len(selected) != CAP:
        raise SystemExit(f"ACS metadata supplied only {len(selected)} usable variables; expected {CAP}")

    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(os.path.join(OUT, "*.md")):
        if os.path.basename(f) != "_access.md":
            os.remove(f)

    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import repr_queries, descriptions
    by_key = {_item_key(dataset, code): (dataset, code, value)
              for dataset, code, value in selected}

    # The formulaic one-liner below ("ACS variable DP03_0128E: concept (label)") says nothing about
    # what the variable measures or which geographies it is reported for, so near-identical ACS
    # labels are indistinguishable at discovery. Expand each into a full description.
    scope = descriptions.scope_for("census")
    detail = descriptions.for_items(
        [(_description_key(dataset, code), clean_label(value["label"]),
          f"ACS 5-year {dataset.title()} variable {code}: {value.get('concept')} "
          f"({clean_label(value['label'])})")
         for dataset, code, value in selected],
        "US Census ACS variable", scope)

    def write_leaf(item_key, queries):                        # called per variable as its queries land
        dataset, code, v = by_key[item_key]
        clean = clean_label(v["label"])
        fm = {
            "type": "Census Variable",
            "title": f"{clean} — US Census ACS",
            "description": (detail.get(_description_key(dataset, code))
                            or f"ACS 5-year {dataset.title()} variable {code}: "
                               f"{v.get('concept')} ({clean})."),
            "tags": ["census", "acs", dataset, "demographics", "community", "needs-assessment"],
            "source": "./_access.md",
            "dataset": dataset,
            "get": f"NAME,{code}",
            "key": "env:CENSUS_API_KEY",
            "variable": code,
            "representativeQueries": queries,
        }
        body = (f"# Schema\n\nACS 5-year {dataset.title()} variable `{code}` — {clean}. Returns the value "
                f"for the requested geography (`geo`); `get`/`key` pinned. See "
                f"[Census access](./_access.md).\n")
        with open(os.path.join(OUT, code.lower().replace("_", "-") + ".md"), "w", encoding="utf-8") as fh:
            fh.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body)

    print(f"generating queries + writing {len(selected)} variable leaves incrementally…")
    repr_queries.for_items(
        [(_item_key(dataset, code), clean_label(value["label"]),
          f"{value.get('concept')} — {clean_label(value['label'])}")
         for dataset, code, value in selected],
        "US Census ACS variable", on_ready=write_leaf)
    print(f"wrote {len(glob.glob(os.path.join(OUT, '*.md'))) - 1} census variable entries to {OUT}")


if __name__ == "__main__":
    main()
