#!/usr/bin/env python3
"""Generate per-concept OKF leaf entries for SEC EDGAR from the CANONICAL
us-gaap taxonomy — not from any sampled company list.

The entry set therefore depends only on the taxonomy (a bounded, company-
independent vocabulary): one OKF leaf per reportable numeric concept. Onboarding
new issuers adds zero entries; the company is a query parameter (`cik`).

Pipeline (nothing hardwired except the official FASB taxonomy host):
  1. discover the latest published us-gaap taxonomy year from the FASB index,
  2. parse the element XSD  -> reportable numeric concepts (+ periodType),
  3. parse the label linkbase -> standard human label,
  4. parse the documentation linkbase -> concept definition,
  5. write one OKF leaf per concept into sources/sec-edgar/.
"""
import os, re, glob, urllib.request

OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "sources", "sec-edgar"))
INDEX = "https://xbrl.fasb.org/us-gaap/"
# value type -> the family of UNIT the concept is reported in. The XBRL response's `units` dict is
# keyed by unit (USD, "USD/shares", shares, pure); the family tells the fetcher which key to read,
# so a per-share or share-count concept isn't mistakenly looked up under USD.
UNIT_FAMILY = {
    "xbrli:monetaryItemType": "currency",       # USD (or the filer's reporting currency)
    "dtr-types:perShareItemType": "per-share",  # e.g. USD/shares
    "xbrli:sharesItemType": "shares",           # a share count
    "dtr-types:percentItemType": "percent",     # a ratio reported as pure
    "xbrli:pureItemType": "pure",
    "xbrli:integerItemType": "pure",
    "xbrli:decimalItemType": "pure",
}
NUMERIC = set(UNIT_FAMILY)                       # value types that represent a queryable quantity


def get(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def latest_year():
    years = sorted({int(y) for y in re.findall(r'href="(20\d\d)/"', get(INDEX))}, reverse=True)
    for y in years:                                  # pick newest year whose XSD exists
        try:
            urllib.request.urlopen(f"{INDEX}{y}/elts/us-gaap-{y}.xsd", timeout=20).read(1)
            return y
        except Exception:
            continue
    raise SystemExit("no usable us-gaap taxonomy year found")


def reportable_concepts(xsd):
    """name -> (periodType, unitFamily), for non-abstract numeric item elements."""
    out = {}
    for tag in re.findall(r"<xs:element\b[^>]*/>", xsd):
        attrs = dict(re.findall(r"(\w[\w:]*)='([^']*)'", tag))
        if attrs.get("abstract") == "true":
            continue
        if attrs.get("substitutionGroup") != "xbrli:item":
            continue
        if attrs.get("type") not in NUMERIC:
            continue
        name = attrs.get("name")
        if name:
            out[name] = (attrs.get("xbrli:periodType", "duration"), UNIT_FAMILY[attrs["type"]])
    return out


def labels_for_role(xml, role):
    """concept -> text, from <link:label> resources with the given role."""
    out = {}
    pat = re.compile(r"<link:label\b([^>]*)>(.*?)</link:label>", re.S)
    for attrs, text in pat.findall(xml):
        a = dict(re.findall(r"xlink:(\w+)='([^']*)'", attrs))
        if a.get("role") != role:
            continue
        lab = a.get("label", "")
        if lab.startswith("lab_"):
            out[lab[4:]] = re.sub(r"\s+", " ", text).strip()
    return out


def slug(name):
    s = re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def main():
    import yaml
    import repr_queries
    y = latest_year()
    base = f"{INDEX}{y}/elts"
    print(f"using us-gaap taxonomy {y}")
    concepts = reportable_concepts(get(f"{base}/us-gaap-{y}.xsd"))
    labels = labels_for_role(get(f"{base}/us-gaap-lab-{y}.xml"), "http://www.xbrl.org/2003/role/label")
    docs = labels_for_role(get(f"{base}/us-gaap-doc-{y}.xml"), "http://www.xbrl.org/2003/role/documentation")
    print(f"{len(concepts)} reportable numeric concepts, {len(labels)} labels, {len(docs)} definitions")

    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(os.path.join(OUT, "*.md")):          # idempotent: clear old leaves
        if os.path.basename(f) != "_access.md":
            os.remove(f)

    kept = []                                                 # (concept, period, label, unit)
    for concept, (period, unit) in concepts.items():
        label = labels.get(concept)
        if not label:
            continue
        if "deprecated" in label.lower() or "deprecated" in docs.get(concept, "").lower():
            continue                                          # drop deprecated us-gaap concepts
        kept.append((concept, period, label, unit))

    info = {c: (period, label, unit) for c, period, label, unit in kept}

    def write_leaf(concept, queries):                          # called per concept as its queries land
        period, label, unit = info[concept]
        fm = {
            "type": "Financial Statement Concept",
            "title": f"{label} — SEC EDGAR",
            "description": docs.get(concept, label),
            "tags": ["finance", "sec", "edgar", "us-gaap"] + [w for w in slug(concept).split("-") if len(w) > 2][:4],
            "source": "./_access.md",
            "taxonomy": "us-gaap",
            "concept": concept,
            "periodType": period,
            "unit": unit,
            "representativeQueries": queries,
        }
        body = (f"# Schema\n\nReports the `us-gaap:{concept}` concept ({period}) per company, by fiscal "
                f"period, from SEC filings. Query by `cik` via the linked source's `company_concept` "
                f"operation; see [SEC EDGAR access](./_access.md).\n")
        with open(os.path.join(OUT, slug(concept) + ".md"), "w", encoding="utf-8") as fh:
            fh.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body)

    print(f"generating queries + writing {len(kept)} concept leaves incrementally…")
    repr_queries.for_items([(c, lab, docs.get(c, lab)) for c, _p, lab, _u in kept],
                           "US-GAAP financial statement concept", on_ready=write_leaf)
    print(f"wrote {len(glob.glob(os.path.join(OUT, '*.md'))) - 1} concept entries to {OUT}")


if __name__ == "__main__":
    main()
