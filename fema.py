#!/usr/bin/env python3
"""FEMA OpenFEMA — federal disaster declarations by state (key-free).

Relevant to disaster-relief nonprofits (Red Cross, Habitat) and to community context for
grant applications. Keyed by US STATE; the place mention is normalized to a 2-letter code.
"""
import re, json, urllib.request, urllib.parse
import driver

BASE = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
UA = {"User-Agent": "ard-data-demo/1.0 (guha@guha.com)"}

ABBR = {  # state name -> USPS code (and FIPS -> code)
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
FIPS = {"01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
        "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
        "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
        "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
        "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
        "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
        "54": "WV", "55": "WI", "56": "WY"}


def to_state(place):
    p = str(place or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", p):
        return p.upper()
    digits = re.sub(r"\D", "", p)
    if digits[:2] in FIPS:
        return FIPS[digits[:2]]
    return ABBR.get(p.lower())


def fetch(place, n=50):
    st = to_state(place)
    if not st:
        raise SystemExit(f"FEMA is by US state; could not map {place!r} to a state")
    q = urllib.parse.urlencode({
        "$filter": f"state eq '{st}'", "$top": n, "$orderby": "declarationDate desc",
        "$select": "state,declarationTitle,incidentType,declarationDate,fyDeclared,declarationType"})
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(f"{BASE}?{q}", headers=UA), timeout=30))
    except Exception as e:
        raise SystemExit(f"FEMA error for {st}: {str(e)[:80]}")
    rows = d.get("DisasterDeclarationsSummaries", []) or []
    return {"state": st, "record_count": len(rows), "results": rows,
            "source": "FEMA OpenFEMA (did:web:fema.gov)"}


async def fetch_async(place, n=50, *, context):
    st = to_state(place)
    if not st:
        raise SystemExit(f"FEMA is by US state; could not map {place!r} to a state")
    data = await driver.accessor_async(
        "sources/fema/_access.md", "declarations", state=st, context=context)
    fields = ("state", "declarationTitle", "incidentType", "declarationDate",
              "fyDeclared", "declarationType")
    rows = [{field: row.get(field) for field in fields}
            for row in (data.get("DisasterDeclarationsSummaries") or [])[:n]]
    return {"state": st, "record_count": len(rows), "results": rows,
            "source": "FEMA OpenFEMA (did:web:fema.gov)"}
