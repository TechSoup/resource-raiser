---
type: Data Source
title: IRS Form 990 (BigQuery) — Nonprofit Financials, queryable (access)
description: The IRS Form 990 nonprofit-financials dataset as a queryable SQL table
  (BigQuery public dataset bigquery-public-data.irs_990). Unlike the per-EIN ProPublica
  API, this can RANK, FILTER and AGGREGATE across the whole population.
resource: bigquery-public-data.irs_990
publisher: irs.gov / Google BigQuery public datasets
trust:
  identity: did:web:irs.gov
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
        grain: organization
        order:
          server: true
        population:
          complete: true
        requires_env: GOOGLE_CLOUD_PROJECT
entityType: the population of US tax-exempt nonprofits (501(c) organizations) — for
  ranking, filtering and counting across all filers, not one at a time
---

# About

The SAME IRS Form 990 data as `nonprofit-990`, but as a BigQuery TABLE — so it answers POPULATION questions (which nonprofit has the highest revenue, how many file, which exceed a threshold) that the per-EIN ProPublica API cannot. A server-aggregate capability at ORGANIZATION grain. **Active only when GOOGLE_CLOUD_PROJECT is set** (BigQuery bills queries to a project); otherwise these questions refuse honestly, as before.
