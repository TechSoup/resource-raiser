---
type: Census ACS Population Field (BigQuery)
title: Median Gross Rent by county — US Census ACS (BigQuery)
description: Rank, filter or aggregate US COUNTIES by median gross rent (median_rent).
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
