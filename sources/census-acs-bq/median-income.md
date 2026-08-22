---
type: Census ACS Population Field (BigQuery)
title: Median Household Income by county — US Census ACS (BigQuery)
description: This measure reports the median household income for US counties, indicating
  the middle income level of households within the area. It describes the economic
  conditions of families and individuals living in the county, allowing for comparisons
  of financial health across different regions. Unlike per capita income, which averages
  individual earnings, median household income focuses on the income of entire households,
  providing a different perspective on economic status. The unit is reported in dollars,
  representing the median income level.
tags:
- census
- acs
- county
- bigquery
- ranking
- aggregate
- population
- median-household-income
source: ./_access.md
bq:
  table: bigquery-public-data.census_bureau_acs.county_2018_5yr
  field: median_income
  entity_field: geo_id
  entity_kind: fips
  name_table: bigquery-public-data.geo_us_boundaries.counties
  name_key: geo_id
  name_field: county_name
  unit: USD
  source: US Census ACS county 5-yr (BigQuery census_bureau_acs)
representativeQueries:
- Which county has the highest median household income?
- which counties have the lowest median household income
- rank US counties by median household income
- which counties have median household income above
- top counties by median household income
---

# Schema

Ranks/filters/aggregates US counties by `median_income` (Median Household Income) via BigQuery. See [Census ACS BigQuery access](./_access.md).
