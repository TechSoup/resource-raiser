---
type: SEC Financials Population Field (BigQuery)
title: Net Income by company — SEC financials (BigQuery)
description: Net Income represents the profit of SEC-reporting public companies after
  all expenses, taxes, and costs have been deducted from total revenue. This measure
  is relevant to the population of SEC-reporting public companies, enabling users
  to rank, filter, or count companies based on their profitability. It is different
  from revenue or total assets, as it specifically reflects the bottom line of financial
  performance rather than overall income or asset value. The reporting is done per
  organization for each fiscal year.
tags:
- sec
- edgar
- financials
- bigquery
- ranking
- aggregate
- population
- company
- net-income
source: ./_access.md
bq:
  table: bigquery-public-data.sec_quarterly_financials.quick_summary
  entity_field: company_name
  entity_kind: company
  value_field: value
  group_agg: MAX
  filter: measure_tag='NetIncomeLoss' AND units='USD' AND form='10-K' AND number_of_quarters=4
    AND fiscal_year=2017
  value_max: 1000000000000.0
  unit: USD
  source: SEC financial statements (BigQuery sec_quarterly_financials, FY2017)
representativeQueries:
- Which company has the highest net income?
- most profitable US public companies
- rank public companies by net income
- which companies earned the most profit
- top companies by profit
---

# Schema

Ranks/filters/counts US public companies by Net Income across the whole population, via BigQuery (LONG/EAV — value picked by `measure_tag`, one value per company via MAX). See [SEC financials BigQuery access](./_access.md).
