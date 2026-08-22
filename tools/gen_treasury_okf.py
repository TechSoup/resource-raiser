#!/usr/bin/env python3
"""Generate OKF leaves for US Treasury FiscalData at the CORRECT granularity.

FiscalData datasets are mostly long-format (one value column, many dimension
rows), so "per-field" is wrong for them — it returns an arbitrary row. We handle:
  - single-row-per-date datasets  -> one leaf per numeric FIELD
  - dimensional datasets          -> one leaf per DIMENSION VALUE (pins a filter)
Field labels/types come from each dataset's `meta`.
"""
import os, glob, json, urllib.request
import yaml

OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "sources", "treasury"))
BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
NUMERIC = {"CURRENCY", "PERCENTAGE", "NUMBER"}

# one row per date -> per numeric field
SINGLE = {"v2/accounting/od/debt_to_penny": "Debt to the Penny"}

# long-format -> per dimension value (filtered)
DIMENSIONAL = {
    "v1/accounting/od/rates_of_exchange": {
        "name": "Treasury Reporting Rates of Exchange", "dim": "country_currency_desc",
        "val": "exchange_rate", "what": "exchange rate to the US dollar"},
    "v2/accounting/od/avg_interest_rates": {
        "name": "Average Interest Rate on Treasury Securities", "dim": "security_desc",
        "val": "avg_interest_rate_amt", "what": "average interest rate"},
}


def get(url):
    return json.load(urllib.request.urlopen(url, timeout=60))


def meta(path):
    d = get(f"{BASE}/{path}?page%5Bsize%5D=1")["meta"]
    return d.get("labels", {}), d.get("dataTypes", {})


def distinct_latest(path, dim):
    """Distinct dimension values from the most recent record_date."""
    d = get(f"{BASE}/{path}?fields={dim},record_date&sort=-record_date&page%5Bsize%5D=600")["data"]
    latest = d[0]["record_date"] if d else None
    return sorted({r[dim] for r in d if r.get("record_date") == latest and r.get(dim)})


def write(slug, fm, body_field):
    body = (f"# Schema\n\n{body_field} See [Treasury access](./_access.md).\n")
    with open(os.path.join(OUT, slug + ".md"), "w", encoding="utf-8") as fh:
        fh.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body)


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(os.path.join(OUT, "*.md")):
        if os.path.basename(f) != "_access.md":
            os.remove(f)

    # collect every leaf (key, frontmatter, body) first, then generate queries in bulk
    leaves = []                                                 # (slug, fm, body, qkey)
    items = []                                                  # (qkey, label, definition)
    for path, name in SINGLE.items():
        labels, dtypes = meta(path)
        ds = path.split("/")[-1].replace("_", "-")
        for field, label in labels.items():
            if dtypes.get(field) not in NUMERIC:
                continue
            qkey = f"{path}|{field}"
            leaves.append((f"{ds}-{field.replace('_','-')}", {
                "type": "Treasury Fiscal Field", "title": f"{name}: {label}",
                "description": f"{label} from Treasury FiscalData '{name}'.",
                "tags": ["finance", "treasury", "government", "fiscal", "debt"],
                "source": "./_access.md", "path": path, "tfield": field,
            }, f"Field `{field}` ({label}) from `{path}`, latest value.", qkey))
            items.append((qkey, f"{name}: {label}", f"{label} from US Treasury FiscalData '{name}'."))
    for path, spec in DIMENSIONAL.items():
        ds = path.split("/")[-1].replace("_", "-")
        for value in distinct_latest(path, spec["dim"]):
            vslug = "".join(c if c.isalnum() else "-" for c in value.lower()).strip("-")[:60]
            qkey = f"{path}|{value}"
            leaves.append((f"{ds}-{vslug}", {
                "type": "Treasury Fiscal Series", "title": f"{spec['name']}: {value}",
                "description": f"{spec['what'].capitalize()} for {value} — Treasury FiscalData '{spec['name']}'.",
                "tags": ["finance", "treasury", "government", "fiscal"],
                "source": "./_access.md", "path": path, "tfield": spec["val"],
                "filter": f"{spec['dim']}:eq:{value}",
            }, f"`{spec['val']}` for {spec['dim']}={value} in `{path}`, latest value.", qkey))
            items.append((qkey, f"{spec['name']}: {value}", f"{spec['what'].capitalize()} for {value}."))

    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import repr_queries, descriptions
    by_qkey = {qkey: (slug, fm, body) for slug, fm, body, qkey in leaves}

    scope = descriptions.scope_for("treasury")
    detail = descriptions.for_items([(f"treasury:{k}", lab, defn) for k, lab, defn in items],
                                    "US Treasury FiscalData field or series", scope)

    def write_leaf(qkey, queries):                            # called per leaf as its queries land
        slug, fm, body = by_qkey[qkey]
        fm["representativeQueries"] = queries
        fm["description"] = detail.get(f"treasury:{qkey}") or fm["description"]
        write(slug, fm, body)

    repr_queries.for_items(items, "US Treasury FiscalData field or series", on_ready=write_leaf)
    print(f"wrote {len(leaves)} Treasury entries ({len(SINGLE)} single + {len(DIMENSIONAL)} dimensional datasets) to {OUT}")


if __name__ == "__main__":
    main()
