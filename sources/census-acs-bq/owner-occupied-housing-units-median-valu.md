---
type: Census ACS Population Field (BigQuery)
title: Median Home Value by county — US Census ACS (BigQuery)
description: This measure indicates the median home value for owner-occupied housing
  units in US counties, representing the middle value of homes owned by residents.
  It describes the real estate market and property values within the county, providing
  insights into housing affordability and investment potential. Unlike average home
  value measures, which can be influenced by outliers, the median home value focuses
  on the midpoint, offering a clearer picture of typical home prices. The unit is
  reported in dollars, reflecting the median value of homes.
tags:
- census
- acs
- county
- bigquery
- ranking
- aggregate
- population
- median-home-value
source: ./_access.md
bq:
  table: bigquery-public-data.census_bureau_acs.county_2018_5yr
  field: owner_occupied_housing_units_median_value
  entity_field: geo_id
  entity_kind: fips
  name_table: bigquery-public-data.geo_us_boundaries.counties
  name_key: geo_id
  name_field: county_name
  unit: USD
  source: US Census ACS county 5-yr (BigQuery census_bureau_acs)
representativeQueries:
- Which county has the highest median home value?
- which counties have the lowest median home value
- rank US counties by median home value
- which counties have median home value above
- top counties by median home value
---

# Schema

Ranks/filters/aggregates US counties by `owner_occupied_housing_units_median_value` (Median Home Value) via BigQuery. See [Census ACS BigQuery access](./_access.md).
