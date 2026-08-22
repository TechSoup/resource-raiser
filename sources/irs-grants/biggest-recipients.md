---
type: Grant Graph — Biggest Recipients (ranking)
title: The biggest grant recipients — IRS 990 grant graph
description: This measure reports on the largest organizations that receive grant
  funding, ranking them based on the total amount of grant money they receive or the
  number of different funders supporting them. It specifically focuses on the philanthropic
  grants between US nonprofits and foundations, as reported in IRS Form 990 data.
  Unlike broader measures that may include all types of funding, this data is limited
  to grants received and does not account for other forms of income. The reporting
  is done per organization for the fiscal years 2022 to 2024.
tags:
- grants
- recipients
- philanthropy
- ranking
- most-funded
- population
- who-funds-whom
source: ./_access.md
irsgrants:
  direction: biggest_recipients
representativeQueries:
- Which organizations receive the most grant money?
- Which nonprofits are funded by the most different foundations?
- Who are the biggest grant recipients?
- Which charities get grants from the most funders?
- Rank organizations by total grants received
---

# Schema

Ranks the RECIPIENT side of the grant graph — by total dollars received, or (for "funded by the
most foundations") by the count of distinct funders, which is the recipient's in-degree. The
mirror image of the top-grantmakers ranking. See [the grant graph access doc](./_access.md).
