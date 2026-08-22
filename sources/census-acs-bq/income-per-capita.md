---
type: Census ACS Population Field (BigQuery)
title: Per Capita Income by county — US Census ACS (BigQuery)
description: This measure provides the per capita income for residents of US counties,
  reflecting the average income earned per person. It describes the economic status
  of individuals within the county, allowing for comparisons of financial well-being
  across different areas. Unlike median income measures, which focus on the middle
  point of income distribution, per capita income averages all incomes, including
  those of lower earners. The unit is reported in dollars, representing the average
  income per person.
tags:
- census
- acs
- county
- bigquery
- ranking
- aggregate
- population
- per-capita-income
source: ./_access.md
bq:
  table: bigquery-public-data.census_bureau_acs.county_2018_5yr
  field: income_per_capita
  entity_field: geo_id
  entity_kind: fips
  name_table: bigquery-public-data.geo_us_boundaries.counties
  name_key: geo_id
  name_field: county_name
  unit: USD
  source: US Census ACS county 5-yr (BigQuery census_bureau_acs)
representativeQueries:
- Which county has the highest per capita income?
- which counties have the lowest per capita income
- rank US counties by per capita income
- which counties have per capita income above
- top counties by per capita income
---

# Schema

Ranks/filters/aggregates US counties by `income_per_capita` (Per Capita Income) via BigQuery. See [Census ACS BigQuery access](./_access.md).
