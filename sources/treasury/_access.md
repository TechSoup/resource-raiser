---
type: Data Source
title: US Treasury — FiscalData (access)
description: Shared access for US Treasury FiscalData REST datasets. Per-table entries in this directory cross-link here.
resource: https://api.fiscaldata.treasury.gov/services/api/fiscal_service/
publisher: treasury.gov
trust:
  identity: did:web:treasury.gov
  identityType: did
access:
  auth: none
  operations:
    get:
      method: GET
      url: "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/{path}?{query}"
      capability:
        page: {complete: true}
        population: {complete: true}
entityType: "the US federal government / national public finances (the whole country's fiscal data)"
---

# Query

Each table is reached with the `get` operation. The leaf entry pins the dataset
`path`; the caller supplies `query` — a URL query string (`fields=`, `filter=`,
`sort=` with `-` for descending, `page[size]=`, `page[number]=`). For the most
recent value, `sort=-record_date&page[size]=1` and read the leaf's key field.

Responses are `{ "data": [ ... ] }`; the records are in `data`.

# Matching & caveats

- **The API does not default to the most recent record.** Without
  `sort=-record_date&page[size]=1` you get an arbitrary (typically oldest) page,
  so a "current" figure must always sort descending by `record_date` and take one
  row. Report that `record_date` as the as-of date.
- **Multi-series tables need a `filter` or the answer is an arbitrary series.**
  Several datasets stack many series in one table — exchange rates carry one row
  per currency per quarter, and the debt tables separate public vs.
  intragovernmental holdings. The leaf pins a filter using
  `field:eq:value` syntax (e.g. `country_currency_desc:eq:Euro Zone-Euro`).
  Dropping it silently returns whichever series happens to sort first.
- **Exchange rates are quarterly reporting rates, not live market rates**, and
  are expressed as *units of foreign currency per US dollar* — the reciprocal of
  the direction people often assume.
- All values arrive as **JSON strings**, not numbers; convert before arithmetic.
- This source is keyed by **date, not by an entity** — there is no company,
  place, or organization to resolve.
