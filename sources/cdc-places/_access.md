---
type: Data Source
title: CDC PLACES — Local Community Health (access)
description: Local (county/place) health measures from CDC PLACES — chronic disease, prevention, and health-risk estimates used for community needs assessment.
resource: https://data.cdc.gov/resource/swc5-untb.json
publisher: cdc.gov
trust:
  identity: did:web:cdc.gov
  identityType: did
access:
  auth: none
  operations:
    by_measure:
      method: GET
      url: "https://data.cdc.gov/resource/swc5-untb.json?measureid={measureid}&$q={place}&$limit=5"
      capability:
    top_by_measure:
      method: GET
      url: "https://data.cdc.gov/resource/swc5-untb.json?measureid={measureid}&$select=locationid,locationname,data_value&$where=data_value%20IS%20NOT%20NULL%20AND%20data_value_type=%27Age-adjusted%20prevalence%27&$order=data_value%20DESC&$limit={n}"
      capability:
        page: {complete: true}
        returns: {label: locationname, value: data_value}
        grain: county
        entity_kind: fips
        entity_field: locationid
        rows_per_unit: {county: 1}
# Declarative fetch spec — interpreted by the harness's generic _s_rest (no CDC-specific code).
fetch:
  op: by_measure
  params: {measureid: ~measureid, place: $key}   # ~measureid pinned in the leaf; $key = resolved place
  rows: objects                                  # response is a list of objects
  pick: "first:data_value"                       # first row that actually has a value
  fields:
    place: col:locationname
    measure: "title~ — CDC"
    value: col:data_value
    unit: col:data_value_unit
  source: CDC PLACES (did:web:cdc.gov)
entityType: "a US county, city, or place, for community health measures (e.g. Los Angeles, Cook County)"
---

# Query

`by_measure` filters one health measure (the leaf pins `measureid`) to a place
(`place`, full-text). Each row has `locationname`, `measure`, `data_value`,
`data_value_unit`, `year`. Read the first row that actually carries a
`data_value` — see the caveats below.

# Matching & caveats

- **`$q` is a full-text search across the whole row, not an exact place field.**
  It can therefore match a row for a different place that merely mentions the
  term. Always check the returned `locationname` against the place asked for
  rather than trusting the filter.
- **Place names here omit the "County" suffix.** Querying `Cook County` matches
  poorly where the dataset stores `Cook`; strip a trailing " County" from a
  resolved place label before searching.
- **Rows can be present but empty.** A row may exist with `data_value` null
  (suppressed for small populations), so scan for the first row that has a
  value instead of blindly taking `[0]`.
- **Every county appears TWICE** — once as `Crude prevalence` and once as
  `Age-adjusted prevalence`. Filtering on `data_value_type` is REQUIRED or the row
  you get is arbitrary. `top_by_measure` pins **age-adjusted**, because crude
  prevalence tracks a county's age structure, which itself correlates with income —
  so crude figures would confound any cross-county comparison or correlation.
- Always report `data_value_unit` with the number: most measures are a
  **percentage of adults**, i.e. a model-based prevalence *rate*, never a count
  of people. Presenting one as a headcount is a category error.
- These are **small-area model-based estimates**, not direct measurements — they
  are modelled from BRFSS survey data down to the local level.
- The dataset id `swc5-untb` pins one specific PLACES release; the `year` field
  reflects the underlying survey year, which lags the release.
