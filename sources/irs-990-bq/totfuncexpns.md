---
type: Nonprofit 990 Population Field (BigQuery)
title: Total Expenses — IRS 990 population (BigQuery)
description: This measure reports the total expenses incurred by US tax-exempt nonprofits,
  allowing for ranking, filtering, or counting across all filers. It focuses solely
  on total expenses, which encompasses all costs associated with the organization's
  operations, distinguishing it from total revenue or net assets. The data is provided
  per organization for each fiscal year, reflecting the financial outflows of nonprofits.
tags:
- nonprofit
- irs
- form-990
- bigquery
- ranking
- aggregate
- population
- expenses
source: ./_access.md
bq:
  table: bigquery-public-data.irs_990.irs_990_2017
  field: totfuncexpns
  entity_field: ein
  entity_kind: ein
  name_via: propublica
  unit: USD
  source: IRS Form 990 (BigQuery bigquery-public-data.irs_990, FY2017)
representativeQueries:
- Which nonprofit has the highest expenses?
- the largest nonprofits by expenses
- which nonprofits have expenses over a billion dollars
- rank nonprofits by expenses
- how many nonprofits have expenses above
---

# Schema

Ranks/filters/counts US nonprofits by the 990 field `totfuncexpns` (Total Expenses) across the whole population, via BigQuery. See [IRS 990 BigQuery access](./_access.md).
