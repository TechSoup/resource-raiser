---
type: Data Source
title: IRS Form 990 — Nonprofit Financials (access)
description: Shared access for US nonprofit financial data from IRS Form 990 filings, via the ProPublica Nonprofit Explorer API. Per-field entries cross-link here.
resource: https://projects.propublica.org/nonprofits/api/v2/
publisher: propublica.org
trust:
  identity: https://projects.propublica.org/nonprofits/
  identityType: https
access:
  auth: none
  operations:
    search:
      method: GET
      url: "https://projects.propublica.org/nonprofits/api/v2/search.json?q={q}"
      capability:
    organization:
      method: GET
      url: "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
      capability:
        period: {field: tax_prd_yr, multi: true}
entityType: "a US nonprofit's FINANCIAL figures from its IRS Form 990 — total revenue, expenses, assets, net assets, contributions & grants received, program service revenue, officer/employee compensation, and its 501(c)(3) filing status (NOT its registration facts or its mission)"
---

# About

US tax-exempt organizations (501(c)) file IRS Form 990 annually. The key is the
**EIN** (Employer Identification Number) — the nonprofit analog of a company's
CIK. Each per-field entry in this directory pins one 990 line item; the caller
supplies the organization.

# Query

1. `search` with `q=<organization name>` → returns matching orgs with `ein`,
   `name`, `city`, `state`, `ntee_code`. Take the top `ein`.
2. `organization` with `ein=<ein>` → returns `filings_with_data[]`, one per year
   (`tax_prd_yr`), each carrying the 990 numeric fields. Pick the year, read the
   field. See the leaf entry for which field.

# Matching & caveats

- **`search` is a fuzzy name match and the top hit is not guaranteed correct.**
  Large charities register many similarly-named affiliates and local chapters,
  each with its own EIN, so the first result may be a chapter rather than the
  national organization. Check the returned `name` (and city/state) before
  trusting the EIN.
- **An organization can exist here with no financial data.** `filings_with_data`
  is empty for 990-N ("e-postcard") filers and other very small organizations,
  even though the org record itself resolves fine. That is a legitimate
  "no data" answer, not a lookup failure.
- **Filings lag by one to two years**, so the latest available `tax_prd_yr` is
  not the current year. Always report the fiscal year alongside the figure.
- **Field coverage varies by form type.** Check `formtype`: a 990-EZ (short form)
  or 990-PF (private foundation) does not populate the same fields as a full 990,
  so a null can mean "not applicable to this form" rather than zero.
- EINs appear in two shapes — dashed (`53-0196605`) from Wikidata and elsewhere,
  bare integer from this API. Strip the dash before calling `organization`.
- Amounts are **as-filed by the organization** and are not audited or restated.
- The org-level fields (location, NTEE, foundation type, deductibility, ruling
  date) come from the IRS Business Master File and are described separately in
  [Nonprofit BMF access](../nonprofit-bmf/_access.md).
