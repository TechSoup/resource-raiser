---
type: Nonprofit 990 Population Field (BigQuery)
title: Net Assets — IRS 990 population (BigQuery)
description: This measure counts the net assets of US tax-exempt nonprofits, enabling
  users to rank, filter, or count across all filers. It specifically refers to net
  assets, which represent the difference between total assets and total liabilities,
  distinguishing it from total revenue or total expenses. The reporting is done per
  organization for each fiscal year, providing insights into the financial stability
  of nonprofits.
tags:
- nonprofit
- irs
- form-990
- bigquery
- ranking
- aggregate
- population
- net-assets
source: ./_access.md
bq:
  table: bigquery-public-data.irs_990.irs_990_2017
  field: totnetassetend
  entity_field: ein
  entity_kind: ein
  name_via: propublica
  unit: USD
  source: IRS Form 990 (BigQuery bigquery-public-data.irs_990, FY2017)
representativeQueries:
- Which nonprofit has the highest net assets?
- the largest nonprofits by net assets
- which nonprofits have net assets over a billion dollars
- rank nonprofits by net assets
- how many nonprofits have net assets above
---

# Schema

Ranks/filters/counts US nonprofits by the 990 field `totnetassetend` (Net Assets) across the whole population, via BigQuery. See [IRS 990 BigQuery access](./_access.md).
