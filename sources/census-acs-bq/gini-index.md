---
type: Census ACS Population Field (BigQuery)
title: Income Inequality (Gini Index) by county — US Census ACS (BigQuery)
description: This measure reports the Gini Index, a statistical representation of
  income inequality within US counties. It specifically describes the distribution
  of income among residents, indicating how evenly or unevenly income is spread across
  the population. Unlike broader measures of income or wealth, the Gini Index focuses
  solely on inequality rather than total income levels. The unit of measurement is
  a ratio ranging from 0 to 1, with higher values indicating greater inequality.
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
