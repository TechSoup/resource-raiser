---
type: Data Source
title: SEC EDGAR — XBRL Company Facts (access)
description: Shared access definition for SEC EDGAR XBRL financial facts. Per-concept entries in this directory cross-link here for their query mechanics.
resource: https://data.sec.gov/api/xbrl/
publisher: sec.gov
trust:
  identity: did:web:sec.gov
  identityType: did
access:
  auth: none
  headers:
    User-Agent: "ard-data-demo (guha@guha.com)"
  operations:
    resolve_ticker:
      method: GET
      url: "https://www.sec.gov/files/company_tickers.json"
      capability:
        population: {complete: true}
    company_concept:
      method: GET
      url: "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}/us-gaap/{concept}.json"
      capability:
        period: {field: end, multi: true}
        grain: company-period
        rows_per_unit: {company: 1}
    company_facts:
      method: GET
      url: "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
      capability:
entityType: "a publicly traded company / SEC filer (e.g. Apple, Microsoft, Tesla)"
---

# About

SEC EDGAR exposes the financial facts of every US public company as XBRL. There
are hundreds of concepts ("tables") per company; each is published as its own
OKF leaf entry in this directory so ARD can retrieve only the relevant one.
The leaf supplies the `concept`; the caller supplies the company (`cik`).

# Query

Identifiers are **CIK** (Central Index Key). The accessor zero-pads `cik` to 10
digits (`{cik:0>10}`), so pass the bare number.

1. Resolve a company name/ticker to a CIK with `resolve_ticker` (returns a map of
   `{ticker, cik_str, title}`); read `cik_str`.
2. Call `company_concept` with `cik=<cik_str>` (the leaf entry already pins
   `concept`).
3. Pick the annual figure from the right **unit** bucket — see the caveats below;
   it is not always `units.USD`.

`company_facts` returns *all* concepts for a company at once — useful to discover
which concept answers a question when it isn't obvious.

# Matching & caveats

- **The response is keyed by UNIT, not just USD.** `units` may contain `USD`,
  `USD/shares`, `shares`, or `pure`. Reading a per-share concept (EPS) or a share
  count out of `units.USD` yields the wrong number or nothing at all. Each leaf
  declares a `unit` family (`currency`, `per-share`, `shares`, `percent`/`pure`)
  and the matching key must be selected. A filer may also report in a non-USD
  currency, so fall back to any 3-letter currency code rather than assuming USD.
- **Do not rely on `frame` (`CY<year>`) to select an annual figure.** It is absent
  for companies whose fiscal year is not the calendar year. Select instead by
  `form` in (`10-K`, `20-F`), a duration of roughly 350–380 days for
  duration-type concepts, and the fiscal year taken from `end[:4]`.
- **A 404 is normal, not an error condition.** It means this company does not
  report that concept; treat it as "try the next candidate concept" rather than a
  failure.
- **Discontinued concepts can shadow current ones.** A company may have filed a
  legacy concept (e.g. `Revenues`) for years and since moved to a current one for
  the same measure. For a "latest" request, prefer the freshest `period_end`
  among the top-ranked concepts the company actually reports, or an abandoned
  concept will answer in place of the live one.
- **A descriptive `User-Agent` is mandatory.** SEC blocks requests without one;
  it is set in this document's `access.headers` and must not be dropped.
- `cik` is zero-padded to 10 digits by the URL template (`{cik:0>10}`) — pass the
  bare number, not a pre-padded string.

# Citations

[1] [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
