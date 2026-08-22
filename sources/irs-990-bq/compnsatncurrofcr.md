---
type: Nonprofit 990 Population Field (BigQuery)
title: Officer Compensation — IRS 990 population (BigQuery)
description: This measure reports the total compensation paid to officers of US tax-exempt
  nonprofits, allowing for ranking, filtering, or counting across all filers. It specifically
  focuses on officer compensation, distinguishing it from other financial metrics
  such as total expenses or revenue. The data is presented as a count per organization
  for each fiscal year, providing insights into compensation practices within the
  nonprofit sector.
tags:
- nonprofit
- irs
- form-990
- bigquery
- ranking
- aggregate
- population
- officer-compensation
source: ./_access.md
bq:
  table: bigquery-public-data.irs_990.irs_990_2017
  field: compnsatncurrofcr
  entity_field: ein
  entity_kind: ein
  name_via: propublica
  unit: USD
  source: IRS Form 990 (BigQuery bigquery-public-data.irs_990, FY2017)
representativeQueries:
- Which nonprofit has the highest officer compensation?
- the largest nonprofits by officer compensation
- which nonprofits have officer compensation over a billion dollars
- rank nonprofits by officer compensation
- how many nonprofits have officer compensation above
---

# Schema

Ranks/filters/counts US nonprofits by the 990 field `compnsatncurrofcr` (Officer Compensation) across the whole population, via BigQuery. See [IRS 990 BigQuery access](./_access.md).
