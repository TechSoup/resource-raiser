---
type: Census ACS Population Field (BigQuery)
title: Income Inequality (Gini Index) by county — US Census ACS (BigQuery)
description: Rank, filter or aggregate US COUNTIES by income inequality (gini index)
  (gini_index).
tags:
- census
- acs
- county
- bigquery
- ranking
- aggregate
- population
- income-inequality
source: ./_access.md
bq:
  table: bigquery-public-data.census_bureau_acs.county_2018_5yr
  field: gini_index
  entity_field: geo_id
  entity_kind: fips
  name_table: bigquery-public-data.geo_us_boundaries.counties
  name_key: geo_id
  name_field: county_name
  source: US Census ACS county 5-yr (BigQuery census_bureau_acs)
representativeQueries:
- Which county has the highest income inequality?
- which counties have the lowest income inequality
- rank US counties by income inequality
- which counties have income inequality above
- top counties by income inequality
---

# Schema

Ranks/filters/aggregates US counties by `gini_index` (Income Inequality (Gini Index)) via BigQuery. See [Census ACS BigQuery access](./_access.md).
