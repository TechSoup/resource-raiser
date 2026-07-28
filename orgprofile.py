#!/usr/bin/env python3
"""Organization profile facts from Wikidata + Wikipedia (key-free, live).

Descriptive, non-financial data about a nonprofit — when it was founded, where it
is headquartered, how many people it employs, who runs it, its website, and a
plain-English overview. Keyed by the entity's Wikidata QID, which the harness has
already resolved on the cross-source spine (see resolver.py), so no extra lookup.
"""
import re, urllib.request, urllib.parse, json

WD = "https://www.wikidata.org/w/api.php"
WP = "https://en.wikipedia.org/api/rest_v1/page/summary/"
UA = {"User-Agent": "ard-data-demo/1.0 (guha@guha.com)"}

# attr -> (Wikidata property, human label, kind). kind: date|amount|url|entity
PROPS = {
    "inception":    ("P571",  "Year Founded",    "date"),
    "headquarters": ("P159",  "Headquarters",    "entity"),
    "employees":    ("P1128", "Employees",       "amount"),
    "website":      ("P856",  "Official Website", "url"),
    "ceo":          ("P169",  "Chief Executive", "entity"),
    "founder":      ("P112",  "Founder",         "entity"),
}


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.load(r)


def _label(qid):
    try:
        e = _get(f"{WD}?action=wbgetentities&ids={qid}&props=labels&languages=en&format=json")["entities"][qid]
        return (e.get("labels", {}).get("en") or {}).get("value") or qid
    except Exception:
        return qid


def fetch(attr, qid, orgname=None):
    """One profile fact for the entity at `qid`. Raises SystemExit if the fact is absent
    (so the harness backtracks) — Wikidata coverage is uneven, e.g. many orgs lack an employee count."""
    e = _get(f"{WD}?action=wbgetentities&ids={qid}&props=claims|labels|sitelinks&format=json")["entities"][qid]
    orgname = orgname or (e.get("labels", {}).get("en") or {}).get("value") or qid
    base = {"organization": orgname, "qid": qid, "source": "Wikidata + Wikipedia (did:web:wikidata.org)"}

    if attr == "overview":                                    # Wikipedia lead paragraph
        title = (e.get("sitelinks", {}).get("enwiki") or {}).get("title")
        if not title:
            raise SystemExit(f"no Wikipedia article for {orgname}")
        s = _get(WP + urllib.parse.quote(title.replace(" ", "_")))
        if not s.get("extract"):
            raise SystemExit(f"no Wikipedia overview for {orgname}")
        return {**base, "field": "Overview", "value": s["extract"]}

    prop, human, kind = PROPS[attr]
    claims = [c for c in e.get("claims", {}).get(prop, []) if c["mainsnak"].get("datavalue")]
    if not claims:
        raise SystemExit(f"no {human} on Wikidata for {orgname}")
    rec = {**base, "field": human}
    if kind == "date":
        t = claims[0]["mainsnak"]["datavalue"]["value"]
        rec["value"] = (str(t.get("time", "")).lstrip("+")[:4] if isinstance(t, dict) else str(t)[:4])
    elif kind == "amount":
        v = claims[0]["mainsnak"]["datavalue"]["value"]
        rec["value"] = str(v.get("amount") if isinstance(v, dict) else v).lstrip("+")
    elif kind == "url":
        rec["value"] = claims[0]["mainsnak"]["datavalue"]["value"]
    else:                                                     # entity-valued -> resolve labels
        # single-holder roles (leader, HQ): prefer the CURRENT one — a claim with no end-time qualifier (P582)
        current = [c for c in claims if "P582" not in c.get("qualifiers", {})]
        use = current if (attr in ("ceo", "headquarters") and current) else claims
        ids, seen = [], set()
        for c in use:
            v = c["mainsnak"]["datavalue"]["value"]
            i = v.get("id") if isinstance(v, dict) else None
            if i and i not in seen:
                seen.add(i)
                ids.append(i)
        names = [n for n in (_label(i) for i in ids[:4]) if not re.fullmatch(r"Q\d+", n)]  # drop unresolved QIDs
        if not names:
            raise SystemExit(f"no resolvable {human} for {orgname}")
        rec["value"] = ", ".join(names)
    return rec
