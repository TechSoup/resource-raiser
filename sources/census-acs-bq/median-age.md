---
type: Census ACS Population Field (BigQuery)
title: Median Age by county — US Census ACS (BigQuery)
description: Rank, filter or aggregate US COUNTIES by median age (median_age).
tags:
- census
- acs
- county
- bigquery
- ranking
- aggregate
- population
- median-age
source: ./_access.md
bq:
  table: bigquery-public-data.census_bureau_acs.county_2018_5yr
  field: median_age
  entity_field: geo_id
  entity_kind: fips
  name_table: bigquery-public-data.geo_us_boundaries.counties
  name_key: geo_id
  name_field: county_name
  source: US Census ACS county 5-yr (BigQuery census_bureau_acs)
representativeQueries:
- Which county has the highest median age?
- which counties have the lowest median age
- rank US counties by median age
- which counties have median age above
- top counties by median age
---

# Schema

Ranks/filters/aggregates US counties by `median_age` (Median Age) via BigQuery. See [Census ACS BigQuery access](./_access.md).
