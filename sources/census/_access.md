---
type: Data Source
title: US Census — American Community Survey (access)
description: Demographic and socioeconomic estimates from the Census Bureau ACS 5-year API, used by nonprofits for community needs assessment.
resource: https://api.census.gov/data/
publisher: census.gov
trust:
  identity: did:web:census.gov
  identityType: did
access:
  auth: key
  operations:
    acs:
      method: GET
      url: "https://api.census.gov/data/2022/acs/acs5/profile?get={get}&for={geo}&key={key}"
      capability:
        grain: county
        entity_kind: fips
        can_aggregate_to: [county, state, place]
        rows_per_unit: {county: 1, state: 1}
entityType: "a US geographic area — a state, county, city, or place (e.g. California, Los Angeles County)"
---

# Query

`acs` runs an ACS 5-year query. The leaf entry pins `get` (the variable codes)
and the API `key`; the caller supplies `geo` (a Census geography clause, e.g.
`state:06` for California, or `county:037&in=state:06` for Los Angeles County).
Response is an array of arrays: a header row plus data rows.

Common state FIPS: CA=06, NY=36, TX=48, FL=12, IL=17, WA=53, MA=25.

# Matching & caveats

- **ACS encodes "no data" as a large negative number, not as null.** Suppressed or
  unavailable estimates come back as *jam values* such as `-666666666`,
  `-888888888`, or `-999999999`. Taken at face value these look like real
  (enormous negative) statistics, so any value at or below about `-100000000`
  must be treated as missing and the query retried at a coarser geography.
- **Response shape is an array of arrays**, not objects: row 0 is the header, row
  1+ are data. For a single-variable query the estimate is at `[1][1]` and the
  place name at `[1][0]`.
- **Not every variable is published for every geography.** Small places are
  frequently suppressed. Back off through the containment chain — place →
  containing county → state — rather than reporting no answer; a broader
  containing area is a reasonable proxy as long as the answer says so.
- Geography clauses differ by level: `state:NN`, `county:CCC&in=state:NN`,
  `place:PPPPP&in=state:NN`. County and place FIPS are only unique *within* a
  state, so the `in=state:` qualifier is required.
- **`auth: key`** — this source needs an API key, unlike the other sources here.
- These are 5-year rolling estimates: the value is an average over a period, not a
  point-in-time count, and it carries a margin of error.
