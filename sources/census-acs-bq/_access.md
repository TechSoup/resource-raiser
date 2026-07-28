---
type: Data Source
title: US Census ACS (BigQuery) — county statistics, queryable (access)
description: American Community Survey county-level statistics as a queryable BigQuery
  table (census_bureau_acs) — RANK, FILTER and AGGREGATE across all ~3,200 US counties,
  which the per-place Census API cannot.
resource: bigquery-public-data.census_bureau_acs
publisher: census.gov / Google BigQuery public datasets
trust:
  identity: did:web:census.gov
  identityType: did
access:
  auth: gcp
  operations:
    query:
      method: BIGQUERY
      url: ''
      capability:
        paths:
        - key
        - filter
        - order
        - enumerate
        - aggregate
        grain: county
        order:
          server: true
        population:
          complete: true
        requires_env: GOOGLE_CLOUD_PROJECT
entityType: the population of US counties — for ranking, filtering and counting community
  statistics across all counties, not one at a time
---

# About

The SAME ACS demographics as `census`, but as a BigQuery TABLE — so it answers POPULATION questions across all US counties (which county has the highest X, counties above a threshold, correlations) that the per-place API cannot. Server-aggregate at COUNTY grain; county names via a join to `geo_us_boundaries.counties`. **Active only when GOOGLE_CLOUD_PROJECT is set.**
