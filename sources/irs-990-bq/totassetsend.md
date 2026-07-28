---
type: Nonprofit 990 Population Field (BigQuery)
title: Total Assets — IRS 990 population (BigQuery)
description: Rank, filter or count US nonprofits by total assets (totassetsend) across
  ALL filers.
tags:
- nonprofit
- irs
- form-990
- bigquery
- ranking
- aggregate
- population
- assets
source: ./_access.md
bq:
  table: bigquery-public-data.irs_990.irs_990_2017
  field: totassetsend
  entity_field: ein
  entity_kind: ein
  name_via: propublica
  unit: USD
  source: IRS Form 990 (BigQuery bigquery-public-data.irs_990, FY2017)
representativeQueries:
- Which nonprofit has the highest assets?
- the largest nonprofits by assets
- which nonprofits have assets over a billion dollars
- rank nonprofits by assets
- how many nonprofits have assets above
---

# Schema

Ranks/filters/counts US nonprofits by the 990 field `totassetsend` (Total Assets) across the whole population, via BigQuery. See [IRS 990 BigQuery access](./_access.md).
