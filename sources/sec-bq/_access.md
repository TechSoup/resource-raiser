---
type: Data Source
title: SEC financial statements (BigQuery) — public-company financials, queryable
  (access)
description: SEC 10-K/10-Q XBRL financial facts as a queryable BigQuery table (sec_quarterly_financials)
  — RANK, FILTER and AGGREGATE revenue / net income / assets across ALL public companies,
  which the per-company EDGAR companyconcept API cannot.
resource: bigquery-public-data.sec_quarterly_financials
publisher: sec.gov / Google BigQuery public datasets
trust:
  identity: did:web:sec.gov
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
        grain: company
        order:
          server: true
        population:
          complete: true
        requires_env: GOOGLE_CLOUD_PROJECT
entityType: the population of SEC-reporting public companies — for ranking, filtering
  and counting by reported financials across all filers, not one company at a time
---

# About

The SAME SEC XBRL financials as the per-company EDGAR `sec-edgar` source, but as a BigQuery TABLE — so it answers POPULATION questions across all public companies (which company has the highest revenue, companies above a threshold, how many report) that the per-CIK API cannot. Server-aggregate at COMPANY grain. **Active only when GOOGLE_CLOUD_PROJECT is set.**

# Matching & caveats

`sec_quarterly_financials.quick_summary` is a LONG/EAV fact table, so a clean population ranking needs de-noising that the leaf `filter` encodes:

- **pick ONE reporting basis**: `form='10-K' AND fiscal_year=2017`, and `number_of_quarters=4` for flow measures (revenue, net income — a full-year duration) vs `number_of_quarters=0` for stock measures (assets — an instant balance).
- **collapse restatements/amendments**: a company files the same fact several times; `group_agg: MAX` takes one value per `company_name` so the population is companies, not filings.
- **span the measure's tag variants**: post-ASC-606, revenue is reported under several us-gaap tags; the filter lists them and MAX picks the company's headline figure.
- **cap filing typos**: `value_max` drops the occasional $10-trillion fat-finger.
