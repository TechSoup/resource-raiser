---
type: Data Source
title: NIH RePORTER — Research Grants (access)
description: NIH-funded research projects and grant awards by organization, from NIH RePORTER.
resource: https://api.reporter.nih.gov/v2/
publisher: nih.gov
trust:
  identity: did:web:nih.gov
  identityType: did
access:
  auth: none
  operations:
    projects_by_org:
      method: POST
      url: "https://api.reporter.nih.gov/v2/projects/search"
      body: '{"criteria":{"org_names":["$org"],"fiscal_years":[$fy]},"include_fields":["ProjectTitle","AwardAmount","Organization","FiscalYear"],"limit":500,"offset":$offset,"sort_field":"award_amount","sort_order":"desc"}'
      capability:
        identity_field: organization.org_name
        # Completeness is SCOPE-DEPENDENT. One organisation's projects (Johns Hopkins FY2024 =
        # 1,828) sit far under the 15k offset ceiling, so an ENTITY-scoped total is exhaustible by
        # paging. The whole POPULATION (~83.5k) is not. Declaring one flat `complete` conflated
        # these and made a fixable truncation look unfixable.
        page: {max: 500, offset_param: offset, complete_for: entity, complete: false}
        # entity-scope completeness ALSO needs a time window: one org across ALL years
        # (Johns Hopkins = 64,417 projects) again exceeds the ceiling. Scoped to one
        # fiscal year it is 1,828 -> exhaustible. Hence the required `fy` parameter.
        scope: {required: [fy], note: one fiscal year per query}
        # ordered scan exists but tops out at ~15k of ~83.5k projects -> BIASED, not merely
        # partial, so it is deliberately NOT marked complete and cannot serve ranking.
        population: {complete: false, ceiling: 15000, size: 83500, partition: org_states}
        grain: project
        can_aggregate_to: []
        # one row per PROJECT, but questions are asked per state/organization -> large blowup.
        # Materialise once per fiscal year rather than paying this per question.
        rows_per_unit: {state: 1670, organization: 1800}
        materializable: {partition: org_states, max_slice: 15000, vintage: fiscal_year}
entityType: "a research organization or university that receives NIH research funding"
---

# Query

`projects_by_org` returns NIH-funded research projects for an organization. Set
`org` to the organization name (e.g. a university or research nonprofit); read
`results` (each has `award_amount`, `project_title`, `fiscal_year`). Use
`--extract results`.

# Matching & caveats

- **Matched by name, not by key.** `criteria.org_names` matches against the
  official registered organization name. It matches on a leading fragment
  (`Stanford` → *STANFORD UNIVERSITY*) but **not** on abbreviations or nicknames
  (`Caltech` → 0 results; the official name *California Institute of Technology*
  is required). Pass the organization's canonical name — the harness resolves the
  mention to its canonical label first. See the `identity` block on
  `research-grants.md`.
- Unlike NSF, this endpoint does **not** silently over-match: a name that does not
  match returns zero rows rather than unrelated organizations.
- **Totals are per FISCAL YEAR, and the year is required.** The operation pages
  every project for one organisation in one fiscal year (`fy`), which is complete:
  Johns Hopkins FY2024 is 1,828 projects, well under the 15k offset ceiling.
  Dropping the year filter spans all years (Johns Hopkins: 64,417 projects), which
  exceeds the ceiling and silently truncates — so `fy` must always be set.
- The identity of each row is nested at `organization.org_name` (not a top-level
  field), which is what the `identity.field` declaration points at.

# Ranking / aggregate queries — NOT supported

**This source answers "how much does organization X get", not "which organization
gets the most".** There is no `aggregate.rankable` declaration on its leaves, so
the harness refuses superlative questions here rather than answering them wrongly.

Why it cannot be done naively:

- **No group-by.** `/v2/projects/search` returns individual *projects*; the
  response `meta` carries no aggregation. Ranking organizations means summing
  projects yourself.
- **The population is large and only partly reachable.** FY2024 alone has ~83,500
  projects, but the API rejects `offset` beyond ~15,000 (`limit` max 500). A
  single sorted sweep can therefore reach only about 18% of the projects.
- **A partial sweep is biased, not merely incomplete.** Summing the largest
  projects favours institutions holding a few very large awards and undercounts
  those funded by many mid-size grants. Aggregating the top 5,000 FY2024 projects
  put Johns Hopkins at ~$277M against an actual annual NIH total of roughly $1B —
  a plausible-looking but materially wrong ordering.
- **The population is not "universities".** The results mix in NIH's own
  intramural divisions (e.g. *Division of Basic Sciences - NCI*) and contractors
  (*Leidos Biomedical Research*, *Research Triangle Institute*). Answering "which
  university" requires an institution-type filter this API does not provide.

A correct implementation would **partition** the query space so each slice stays
under the offset ceiling (e.g. by `org_states`, ~50 slices) and union the results,
then filter to degree-granting institutions — best done as a precomputed
aggregate, not a live per-question fetch.
