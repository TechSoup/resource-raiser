---
type: Census ACS Population Field (BigQuery)
title: Median Gross Rent by county — US Census ACS (BigQuery)
description: This measure provides the median gross rent for housing units in US counties,
  reflecting the middle rent price paid by tenants. It describes the rental market
  conditions and affordability for residents within the county. Unlike average rent
  measures, which can be skewed by extremely high or low rents, the median gross rent
  focuses on the midpoint, offering a more stable indicator of rental costs. The unit
  is reported in dollars, representing the median rent amount.
tags:
- census
- acs
- county
- bigquery
- ranking
- aggregate
- population
- median-rent
source: ./_access.md
bq:
  table: bigquery-public-data.census_bureau_acs.county_2018_5yr
  field: median_rent
  entity_field: geo_id
  entity_kind: fips
  name_table: bigquery-public-data.geo_us_boundaries.counties
  name_key: geo_id
  name_field: county_name
  unit: USD
  source: US Census ACS county 5-yr (BigQuery census_bureau_acs)
representativeQueries:
- Which county has the highest median rent?
- which counties have the lowest median rent
- rank US counties by median rent
- which counties have median rent above
- top counties by median rent
---

# Schema

Ranks/filters/aggregates US counties by `median_rent` (Median Gross Rent) via BigQuery. See [Census ACS BigQuery access](./_access.md).
