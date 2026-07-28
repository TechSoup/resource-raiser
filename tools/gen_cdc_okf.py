#!/usr/bin/env python3
"""Generate one OKF leaf per CDC PLACES health measure (~40), so the agent finder
distinguishes obesity vs diabetes vs uninsured at discovery. Each leaf pins the
`measureid`; the place is a query param. Measures pulled live from the dataset.
"""
import os, glob, json, urllib.request
import yaml

OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "sources", "cdc-places"))
SRC = "https://data.cdc.gov/resource/swc5-untb.json?$select=measureid,measure&$group=measureid,measure&$limit=100"


def short(measure):
    for suf in (" among adults", " among women", " among adults aged 18-64 years", " in the past 12 months"):
        measure = measure.replace(suf, "")
    return measure.strip()


def main():
    measures = json.load(urllib.request.urlopen(SRC, timeout=60))
    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(os.path.join(OUT, "*.md")):
        if os.path.basename(f) != "_access.md":
            os.remove(f)

    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import repr_queries
    kept = [(m["measureid"], m["measure"]) for m in measures if m.get("measureid") and m.get("measure")]
    label_of = dict(kept)

    def write_leaf(mid, queries):                             # called per measure as its queries land
        label = label_of[mid]
        fm = {
            "type": "Community Health Measure",
            "title": f"{label} — CDC PLACES",
            "description": f"Local prevalence estimate: {label} (CDC PLACES, county/place level).",
            "tags": ["nonprofit", "health", "cdc", "places", "community", "needs-assessment"],
            "source": "./_access.md",
            "measureid": mid,
            "representativeQueries": queries,
        }
        body = (f"# Schema\n\nCDC PLACES measure `{mid}` — {label}. Returns the local prevalence "
                f"(`data_value`, `data_value_unit`) for a place (`place`). See "
                f"[CDC PLACES access](./_access.md).\n")
        with open(os.path.join(OUT, mid.lower().replace("_", "-") + ".md"), "w", encoding="utf-8") as fh:
            fh.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body)

    repr_queries.for_items(
        [(mid, short(label), f"Local prevalence estimate: {label} (CDC PLACES).") for mid, label in kept],
        "CDC PLACES community health measure", on_ready=write_leaf)
    print(f"wrote {len(glob.glob(os.path.join(OUT, '*.md'))) - 1} CDC PLACES measure entries to {OUT}")


if __name__ == "__main__":
    main()
