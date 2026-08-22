---
type: Census ACS Population Field (BigQuery)
title: Median Age by county — US Census ACS (BigQuery)
description: This measure indicates the median age of the population in US counties,
  representing the age at which half the residents are younger and half are older.
  It describes the demographic composition of the county, providing insights into
  the age distribution of its residents. Unlike measures that report average age,
  the median age specifically highlights the midpoint, which can be less affected
  by extreme values. The unit is a count of years, reflecting the age of the population.
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
