#!/usr/bin/env python3
"""Retrieval for IRS Form 990 nonprofit financials (ProPublica Nonprofit Explorer).

Mirrors driver.fetch_metric but for nonprofits: resolve org name -> EIN, fetch the
org's filings, pick the year, read the field. HTTP access goes through the generic
OKF accessor (driver.accessor) using sources/nonprofit-990/_access.md.
"""
import re
import driver

ACCESS = "sources/nonprofit-990/_access.md"


def _search_ein(org):
    d = driver.accessor(ACCESS, "search", q=org)   # okf_fetch URL-encodes params
    orgs = d.get("organizations", [])
    return (orgs[0]["ein"], orgs[0]["name"]) if orgs else (None, None)


def resolve(org):
    """Anchor the entity on {ein, name} — the nonprofit join spine. Accepts a name
    OR an EIN, so callers can resolve once and pass either handle onward."""
    if str(org).isdigit():
        d = driver.accessor(ACCESS, "organization", ein=str(org))
        return {"ein": int(org), "name": d["organization"]["name"]}
    ein, name = _search_ein(org)
    if not ein:
        raise SystemExit(f"no nonprofit found for {org!r}")
    return {"ein": ein, "name": name}


async def resolve_async(org, *, context):
    if str(org).isdigit():
        data = await driver.accessor_async(ACCESS, "organization", ein=str(org), context=context)
        return {"ein": int(org), "name": data["organization"]["name"]}
    data = await driver.accessor_async(ACCESS, "search", q=org, context=context)
    organizations = data.get("organizations", [])
    if not organizations:
        raise SystemExit(f"no nonprofit found for {org!r}")
    return {"ein": organizations[0]["ein"], "name": organizations[0]["name"]}


async def federal_funding_async(org, *, context):
    name = (await resolve_async(org, context=context))["name"] if str(org).isdigit() else org
    data = await driver.accessor_async(
        "sources/usaspending/federal-awards-received.md", "awards_by_recipient",
        org=name, context=context)
    awards = data.get("results", []) if isinstance(data, dict) else (data or [])
    total = sum(award.get("Award Amount") or 0 for award in awards)
    return {"organization": name, "federal_awards_total_usd": total, "award_count": len(awards),
            "source": "USAspending.gov", "note": "sum of the largest returned awards"}


async def fetch_np_async(field, org, period="latest", *, context):
    ein = (await resolve_async(org, context=context))["ein"]
    data = await driver.accessor_async(ACCESS, "organization", ein=ein, context=context)
    filings = data.get("filings_with_data", [])
    if not filings:
        raise SystemExit(f"no 990 financial data for {org!r}")
    year = re.sub(r"\D", "", period or "")
    filing = (next((item for item in filings if str(item.get("tax_prd_yr")) == year), None)
              if len(year) == 4 else None)
    filing = filing or max(filings, key=lambda item: item.get("tax_prd_yr", 0))
    return {"organization": data["organization"]["name"], "ein": ein, "field": field,
            "period": f"FY{filing.get('tax_prd_yr')}", "value_usd": filing.get(field),
            "source": "IRS Form 990 (via ProPublica Nonprofit Explorer)"}


async def classify_async(org, *, context):
    resolved = await resolve_async(org, context=context)
    data = await driver.accessor_async(
        ACCESS, "organization", ein=resolved["ein"], context=context)
    organization = data["organization"]
    filings = data.get("filings_with_data", [])
    subsection = filings[0].get("subseccd") if filings else None
    latest = max((filing.get("tax_prd_yr", 0) for filing in filings), default=None)
    return {"organization": organization.get("name", resolved["name"]), "ein": resolved["ein"],
            "subsection": subsection, "is_501c3": subsection == 3,
            "ntee_code": organization.get("ntee_code"), "ruling_date": organization.get("ruling_date"),
            "latest_filing_year": latest, "actively_filing": bool(latest),
            "source": "IRS Form 990 (via ProPublica Nonprofit Explorer)"}


async def bmf_async(field, org, *, context):
    resolved = await resolve_async(org, context=context)
    data = await driver.accessor_async(
        ACCESS, "organization", ein=resolved["ein"], context=context)
    organization = data["organization"]
    base = {"organization": organization.get("name", resolved["name"]), "ein": resolved["ein"],
            "source": "IRS Exempt Organization Business Master File (via ProPublica Nonprofit Explorer)"}
    if field == "location":
        return {**base, "field": "headquarters",
                "value": ", ".join(value for value in
                                    [organization.get("city"), organization.get("state")] if value) or None,
                "address": organization.get("address"), "zipcode": organization.get("zipcode")}
    if field == "ntee":
        code = organization.get("ntee_code") or ""
        return {**base, "field": "ntee_sector", "ntee_code": code or None,
                "value": NTEE_MAJOR.get(code[:1].upper()) if code else None}
    if field == "foundation":
        code = str(organization.get("foundation_code") or "").zfill(2)
        return {**base, "field": "foundation_type", "foundation_code": code,
                "value": FOUNDATION.get(code, "Unclassified")}
    if field == "deductibility":
        code = str(organization.get("deductibility_code") or "")
        return {**base, "field": "deductibility", "deductibility_code": code,
                "value": DEDUCT.get(code, "Unknown")}
    if field == "ruling_date":
        ruling = str(organization.get("ruling_date") or "")
        return {**base, "field": "ruling_date",
                "value": f"{ruling[:4]}-{ruling[4:6]}" if len(ruling) >= 6 else ruling or None}
    if field == "eligibility":
        status = str(organization.get("exempt_organization_status_code") or "")
        subsection = str(organization.get("subsection_code") or "")
        active = status in ("1", "2")
        return {**base, "field": "eligibility_status",
                "exempt_status": EO_STATUS.get(status, f"Unknown (code {status})") if status else None,
                "is_501c3": subsection in ("03", "3"),
                "contributions_deductible": str(organization.get("deductibility_code") or "") == "1",
                "value": ("Active 501(c)(3) in good standing" if active and subsection in ("03", "3")
                          else "Active tax-exempt organization" if active
                          else EO_STATUS.get(status, "status unknown")),
                "eligible_for_nonprofit_programs": active}
    return {**base, "field": field, "value": organization.get(field)}


def federal_funding(org):
    """Federal grant/assistance awards to this org (USAspending), summed. USAspending
    matches by NAME, so resolve an EIN to its name first."""
    name = resolve(org)["name"] if str(org).isdigit() else org
    d = driver.accessor("sources/usaspending/federal-awards-received.md", "awards_by_recipient", org=name)
    awards = d.get("results", []) if isinstance(d, dict) else (d or [])
    total = sum(a.get("Award Amount") or 0 for a in awards)
    return {"organization": name, "federal_awards_total_usd": total, "award_count": len(awards),
            "source": "USAspending.gov", "note": "sum of the largest returned awards"}


def classify(org):
    """Live classification/status for a nonprofit (type, sector, recognition, activity)
    from 990-derived data — no bulk files."""
    r = resolve(org)
    d = driver.accessor(ACCESS, "organization", ein=r["ein"])
    o = d["organization"]
    filings = d.get("filings_with_data", [])
    sub = filings[0].get("subseccd") if filings else None
    latest = max((f.get("tax_prd_yr", 0) for f in filings), default=None)
    return {
        "organization": o.get("name", r["name"]),
        "ein": r["ein"],
        "subsection": sub,
        "is_501c3": sub == 3,
        "ntee_code": o.get("ntee_code"),
        "ruling_date": o.get("ruling_date"),
        "latest_filing_year": latest,
        "actively_filing": bool(latest),
        "source": "IRS Form 990 (via ProPublica Nonprofit Explorer)",
    }


# --- IRS Business Master File (org-level registration facts, decoded) ---
NTEE_MAJOR = {
    "A": "Arts, Culture & Humanities", "B": "Education", "C": "Environment", "D": "Animal-Related",
    "E": "Health Care", "F": "Mental Health & Crisis Intervention", "G": "Voluntary Health Associations",
    "H": "Medical Research", "I": "Crime & Legal-Related", "J": "Employment",
    "K": "Food, Agriculture & Nutrition", "L": "Housing & Shelter",
    "M": "Public Safety, Disaster Preparedness & Relief", "N": "Recreation & Sports",
    "O": "Youth Development", "P": "Human Services", "Q": "International & Foreign Affairs",
    "R": "Civil Rights, Social Action & Advocacy", "S": "Community Improvement",
    "T": "Philanthropy, Voluntarism & Grantmaking", "U": "Science & Technology", "V": "Social Science",
    "W": "Public & Societal Benefit", "X": "Religion-Related", "Y": "Mutual & Membership Benefit",
    "Z": "Unknown",
}
FOUNDATION = {  # IRS foundation codes -> plain type
    "00": "Not a private foundation (public charity or non-501(c)(3))",
    "02": "Private operating foundation", "03": "Private operating foundation", "04": "Private non-operating foundation",
    "10": "Public charity — church", "11": "Public charity — school", "12": "Public charity — hospital",
    "13": "Public charity — supports a college/university", "14": "Public charity — governmental unit",
    "15": "Public charity — publicly supported (170(b)(1)(A)(vi))", "16": "Public charity — 509(a)(2)",
    "17": "Public charity — 509(a)(3) supporting organization", "18": "Public safety testing organization (509(a)(4))",
    "21": "509(a)(3) supporting org — Type I", "22": "509(a)(3) — Type II",
    "23": "509(a)(3) — Type III functionally integrated", "24": "509(a)(3) — Type III non-functionally integrated",
}
DEDUCT = {"1": "Contributions are tax-deductible", "2": "Contributions are NOT tax-deductible",
          "4": "Contributions are deductible by treaty (foreign organization)"}
# IRS exempt-organization status codes — the good-standing signal a validator (e.g. TechSoup)
# checks before granting a nonprofit discount/donation eligibility.
EO_STATUS = {"1": "Active — unconditional exemption", "2": "Active — conditional exemption",
             "12": "Trust described in section 4947(a)(2)", "25": "Exemption revoked"}


def bmf(field, org):
    """Org-level IRS Business Master File facts (registration, location, classification),
    decoded to plain language. Distinct from the 990 filing line items in fetch_np."""
    r = resolve(org)                                          # accepts name or EIN
    o = driver.accessor(ACCESS, "organization", ein=r["ein"])["organization"]
    base = {"organization": o.get("name", r["name"]), "ein": r["ein"],
            "source": "IRS Exempt Organization Business Master File (via ProPublica Nonprofit Explorer)"}
    if field == "location":
        return {**base, "field": "headquarters",
                "value": ", ".join(x for x in [o.get("city"), o.get("state")] if x) or None,
                "address": o.get("address"), "zipcode": o.get("zipcode")}
    if field == "ntee":
        code = (o.get("ntee_code") or "")
        return {**base, "field": "ntee_sector", "ntee_code": code or None,
                "value": NTEE_MAJOR.get(code[:1].upper()) if code else None}
    if field == "foundation":
        code = str(o.get("foundation_code") or "").zfill(2)
        return {**base, "field": "foundation_type", "foundation_code": code,
                "value": FOUNDATION.get(code, "Unclassified")}
    if field == "deductibility":
        code = str(o.get("deductibility_code") or "")
        return {**base, "field": "deductibility", "deductibility_code": code, "value": DEDUCT.get(code, "Unknown")}
    if field == "ruling_date":
        rd = str(o.get("ruling_date") or "")
        return {**base, "field": "ruling_date", "value": f"{rd[:4]}-{rd[4:6]}" if len(rd) >= 6 else (rd or None)}
    if field == "eligibility":
        # The composite "can this org receive a nonprofit discount/donation?" answer a validator wants:
        # recognized 501(c)(3), active exemption, tax-deductible contributions.
        status = str(o.get("exempt_organization_status_code") or "")
        sub = str(o.get("subsection_code") or "")
        active = status in ("1", "2")
        return {**base, "field": "eligibility_status",
                "exempt_status": EO_STATUS.get(status, f"Unknown (code {status})") if status else None,
                "is_501c3": sub == "03" or sub == "3",
                "contributions_deductible": str(o.get("deductibility_code") or "") == "1",
                "value": ("Active 501(c)(3) in good standing" if active and (sub in ("03", "3"))
                          else "Active tax-exempt organization" if active
                          else EO_STATUS.get(status, "status unknown")),
                "eligible_for_nonprofit_programs": active}
    return {**base, "field": field, "value": o.get(field)}


def fetch_np(field, org, period="latest"):
    ein = resolve(org)["ein"]                                # accepts name or EIN
    d = driver.accessor(ACCESS, "organization", ein=ein)
    filings = d.get("filings_with_data", [])
    if not filings:
        raise SystemExit(f"no 990 financial data for {org!r}")
    yr = re.sub(r"\D", "", period or "")
    f = next((x for x in filings if str(x.get("tax_prd_yr")) == yr), None) if len(yr) == 4 else None
    f = f or max(filings, key=lambda x: x.get("tax_prd_yr", 0))
    return {
        "organization": d["organization"]["name"],
        "ein": ein,
        "field": field,
        "period": f"FY{f.get('tax_prd_yr')}",
        "value_usd": f.get(field),
        "source": "IRS Form 990 (via ProPublica Nonprofit Explorer)",
    }
