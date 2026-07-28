---
type: Nonprofit 990 Population Field (BigQuery)
title: Total Revenue — IRS 990 population (BigQuery)
description: Rank, filter or count US nonprofits by total revenue (totrevenue) across
  ALL filers.
tags:
- nonprofit
- irs
- form-990
- bigquery
- ranking
- aggregate
- population
- revenue
source: ./_access.md
bq:
  table: bigquery-public-data.irs_990.irs_990_2017
  field: totrevenue
  entity_field: ein
  entity_kind: ein
  name_via: propublica
  unit: USD
  source: IRS Form 990 (BigQuery bigquery-public-data.irs_990, FY2017)
representativeQueries:
- Which nonprofit has the highest revenue?
- the largest nonprofits by revenue
- which nonprofits have revenue over a billion dollars
- rank nonprofits by revenue
- how many nonprofits have revenue above
---

# Schema

Ranks/filters/counts US nonprofits by the 990 field `totrevenue` (Total Revenue) across the whole population, via BigQuery. See [IRS 990 BigQuery access](./_access.md).
