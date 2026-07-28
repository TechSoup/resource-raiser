---
type: SEC Financials Population Field (BigQuery)
title: Total Assets by company — SEC financials (BigQuery)
description: Rank, filter or count US public companies by total assets across ALL
  SEC filers.
tags:
- sec
- edgar
- financials
- bigquery
- ranking
- aggregate
- population
- company
- total-assets
source: ./_access.md
bq:
  table: bigquery-public-data.sec_quarterly_financials.quick_summary
  entity_field: company_name
  entity_kind: company
  value_field: value
  group_agg: MAX
  filter: measure_tag='Assets' AND units='USD' AND form='10-K' AND number_of_quarters=0
    AND fiscal_year=2017
  value_max: 10000000000000.0
  unit: USD
  source: SEC financial statements (BigQuery sec_quarterly_financials, FY2017)
representativeQueries:
- Which company has the most total assets?
- largest US public companies by assets
- rank public companies by total assets
- which companies have the biggest balance sheet
- top companies by assets
---

# Schema

Ranks/filters/counts US public companies by Total Assets across the whole population, via BigQuery (LONG/EAV — value picked by `measure_tag`, one value per company via MAX). See [SEC financials BigQuery access](./_access.md).
