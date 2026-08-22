#!/usr/bin/env python3
"""Fill in thin descriptions on HAND-AUTHORED OKF leaves, in place.

The generated sources (SEC, Census, Treasury, CDC, 990) get their detailed descriptions from
their `gen_*.py` via tools/descriptions.py. The hand-authored leaves — College Scorecard,
Wikidata profile, BMF, the BigQuery population sources, FEMA/NIH/NSF/USAspending/grants.gov —
have no generator, so this walks them directly.

It rewrites ONLY the `description` field, leaving key order, every other field, and the
Markdown body untouched, and it is idempotent: a leaf already above the length threshold is
skipped, so re-running after a partial pass costs nothing.

  python3 tools/enrich_descriptions.py            # all sources, dry-run summary
  python3 tools/enrich_descriptions.py --write    # write the files
  python3 tools/enrich_descriptions.py --write --sources college-scorecard,fema
  python3 tools/enrich_descriptions.py --write --min-chars 200 --sources sec-edgar
"""
import os, sys, glob, argparse, collections
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
import descriptions

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SOURCES = os.path.join(ROOT, "sources")

# Sources with a generator — their descriptions come from the generator, so re-running it is the
# right way to change them. Listed here so a stray run of this tool cannot fight a generator.
GENERATED = {"sec-edgar", "census", "treasury", "cdc-places", "nonprofit-990"}

DOMAIN = {                                       # what one leaf IS, per source, for the prompt
    "college-scorecard": "US College Scorecard institution field",
    "nonprofit-profile": "descriptive profile field about a nonprofit (from Wikidata)",
    "nonprofit-bmf": "IRS Business Master File registration/classification field",
    "irs-grants": "traversal of the IRS 990 grant graph (who funds whom)",
    "sec-bq": "population-scale SEC financial measure queried over BigQuery",
    "irs-990-bq": "population-scale IRS 990 nonprofit measure queried over BigQuery",
    "census-acs-bq": "population-scale US Census ACS measure queried over BigQuery",
    "usaspending": "US federal award (grant/contract) measure",
    "nih-reporter": "NIH research funding measure",
    "nsf-awards": "NSF research award measure",
    "grants-gov": "US federal grant funding opportunity listing",
    "fema": "FEMA federal disaster declaration dataset",
}


def split(path):
    """(frontmatter dict, raw frontmatter text, body) — body is preserved byte-for-byte."""
    t = open(path, encoding="utf-8").read()
    if not t.startswith("---"):
        return None, None, None
    _, fm_text, body = t.split("---", 2)
    return (yaml.safe_load(fm_text) or {}), fm_text, body


def leaves(source_dirs=None):
    out = []
    for path in sorted(glob.glob(os.path.join(SOURCES, "**", "*.md"), recursive=True)):
        if os.path.basename(path) == "_access.md":
            continue
        src = os.path.relpath(path, SOURCES).split(os.sep)[0]
        if source_dirs and src not in source_dirs:
            continue
        fm, _, _ = split(path)
        if not fm or not fm.get("representativeQueries"):
            continue
        out.append((src, path, fm))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write the files (default: dry run)")
    ap.add_argument("--min-chars", type=int, default=200,
                    help="a description at least this long is already detailed enough (default 200)")
    ap.add_argument("--sources", default="", help="comma-separated source dirs (default: all hand-authored)")
    a = ap.parse_args(argv)

    want = {s.strip() for s in a.sources.split(",") if s.strip()}
    todo = [(src, p, fm) for src, p, fm in leaves(want or None)
            if (src not in GENERATED or src in want)
            and len(fm.get("description") or "") < a.min_chars]
    if not todo:
        print("nothing to enrich — every leaf in scope is already detailed")
        return 0

    by_src = collections.defaultdict(list)
    for src, p, fm in todo:
        by_src[src].append((p, fm))
    print(f"{len(todo)} thin leaves (< {a.min_chars} chars) across {len(by_src)} sources:")
    for src, v in sorted(by_src.items()):
        print(f"  {src:22} {len(v):>5}")
    if not a.write:
        print("\ndry run — pass --write to enrich")
        return 0

    written = 0
    for src, v in sorted(by_src.items()):
        scope = descriptions.scope_for(src)
        by_key = {f"{src}:{os.path.basename(p)}": (p, fm) for p, fm in v}
        detail = descriptions.for_items(
            [(k, fm.get("title", ""), fm.get("description") or fm.get("title", ""))
             for k, (p, fm) in by_key.items()],
            DOMAIN.get(src, f"{src} data table"), scope)
        for k, (p, fm) in by_key.items():
            d = (detail.get(k) or "").strip()
            if not d or d == (fm.get("description") or ""):
                continue
            fm["description"] = d                    # only this field changes
            _, _, body = split(p)
            with open(p, "w", encoding="utf-8") as f:
                f.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---" + body)
            written += 1
        print(f"  {src}: wrote {len(by_key)} leaves")
    print(f"\nenriched {written} leaves — rebuild the index: python3 registry/index.py build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
