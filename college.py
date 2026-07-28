#!/usr/bin/env python3
"""US Dept. of Education — College Scorecard retrieval.

Colleges and universities (mostly nonprofit, and core TechSoup customers) — tuition,
enrollment, admission and completion rates, net price. Keyed by school NAME (the API does
its own fuzzy matching), so no spine resolution is needed. Fields come back as flat dotted
keys (e.g. "latest.cost.tuition.out_of_state"), which is what the leaf pins.
"""
import os, json, urllib.request, urllib.parse

BASE = "https://api.data.gov/ed/collegescorecard/v1/schools"
KEY = os.getenv("DATA_GOV_API_KEY", "DEMO_KEY")     # DEMO_KEY is public + rate-limited; set a real one for volume


def fetch(field, name):
    q = urllib.parse.urlencode({"school.name": name, "fields": f"school.name,{field}",
                                "per_page": 1, "api_key": KEY})
    try:
        d = json.load(urllib.request.urlopen(f"{BASE}?{q}", timeout=25))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise SystemExit("College Scorecard rate-limited — the shared DEMO_KEY is throttled; "
                             "set a free api.data.gov key in DATA_GOV_API_KEY for higher limits")
        raise SystemExit(f"College Scorecard error for {name!r}: HTTP {e.code}")
    except Exception as e:
        raise SystemExit(f"College Scorecard error for {name!r}: {str(e)[:80]}")
    res = d.get("results") or []
    if not res:
        raise SystemExit(f"no College Scorecard match for {name!r}")
    r = res[0]
    return {"school": r.get("school.name", name), "field": field, "value": r.get(field),
            "source": "US Dept. of Education — College Scorecard (did:web:ed.gov)"}
