---
type: Data Source
title: IRS 990 Grant Graph — who funds whom (access)
description: The civil-society grant graph — every grant one US nonprofit or foundation
  makes to another, extracted from IRS Form 990 e-file filings (Schedule I for public
  charities, 990-PF Part XV for private foundations) for 2022-2024. Traversable in both
  directions — the grants an organization MADE, and the funders that GAVE to it.
resource: IRS 990 e-file XML (Schedule I + 990-PF Part XV), 2022-2024
publisher: irs.gov (990 e-file) / extracted locally
trust:
  identity: did:web:irs.gov
  identityType: did
access:
  auth: none
  operations:
    graph:
      method: LOCAL
      url: ''
      capability:
        paths:
        - key
        - order
        grain: organization
        order:
          server: true
        population:
          complete: true
entityType: philanthropic grants between US nonprofits and foundations (IRS Form 990 grant
  data, NOT federal awards) — for who funds whom, grants an org made or received, the biggest
  grantmakers and biggest grant recipients, grant money by state, grant money by cause
  (education, health, environment, …), shared grantees between funders, and overall grant totals
---

# About

A local GRAPH of grant edges — `funder -> recipient (amount, purpose, year)` — built from IRS
990 e-file filings by `tools/grants_etl.py`. This is the relationship layer the per-org APIs cannot
express: ProPublica/BMF describe one organization; this describes the money flowing BETWEEN them,
and it reads in both directions.

# Matching & caveats

- **Two form sources.** Schedule I (public charities granting to organizations) carries the
  recipient's EIN, so reverse lookup ("who funds Stanford?") is a clean EIN join. 990-PF (private
  foundations) lists recipients by name and state with NO EIN, so foundation-side reverse lookups
  fall back to a recipient-name match, and the result says which method was used.
- **Coverage is 2022-2024 filings**, which (because 990s are filed on a lag) is mostly fiscal years
  ~2021-2023; the newest year is still maturing. Org-to-org grants only — grants to individuals
  (scholarships, RecipientPersonNm) are excluded so the graph is organization-to-organization.
- **Amounts are the cash grant paid** (Schedule I `CashGrantAmt`, 990-PF `Amt`); non-cash/in-kind
  rows appear as $0 and are excluded from the ranked dollar figures.
