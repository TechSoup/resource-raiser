---
type: SEC Financials Population Field (BigQuery)
title: Revenue by company — SEC financials (BigQuery)
description: Rank, filter or count US public companies by revenue across ALL SEC filers.
tags:
- sec
- edgar
- financials
- bigquery
- ranking
- aggregate
- population
- company
- revenue
source: ./_access.md
bq:
  table: bigquery-public-data.sec_quarterly_financials.quick_summary
  entity_field: company_name
  entity_kind: company
  value_field: value
  group_agg: MAX
  filter: measure_tag IN ('Revenues','RevenueFromContractWithCustomerExcludingAssessedTax','RevenueFromContractWithCustomerIncludingAssessedTax')
    AND units='USD' AND form='10-K' AND number_of_quarters=4 AND fiscal_year=2017
  value_max: 1000000000000.0
  unit: USD
  source: SEC financial statements (BigQuery sec_quarterly_financials, FY2017)
representativeQueries:
- Which company has the highest revenue?
- largest US public companies by revenue
- rank public companies by revenue
- which companies have revenue over 100 billion
- top companies by revenue
---

# Schema

Ranks/filters/counts US public companies by Revenue across the whole population, via BigQuery (LONG/EAV — value picked by `measure_tag`, one value per company via MAX). See [SEC financials BigQuery access](./_access.md).
