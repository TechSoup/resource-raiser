---
type: Data Source
title: NSF Awards — Research Grants (access)
description: National Science Foundation research grant awards by awardee organization.
resource: https://api.nsf.gov/services/v1/
publisher: nsf.gov
trust:
  identity: did:web:nsf.gov
  identityType: did
access:
  auth: none
  operations:
    awards_by_awardee:
      method: GET
      url: "https://api.nsf.gov/services/v1/awards.json?awardeeName=%22{awardee}%22&printFields=title,fundsObligatedAmt,awardeeName,date,startDate"
      capability:
        identity_field: awardeeName
        page: {max: 25, complete: false}
        grain: award
        rows_per_unit: {organization: 25}
entityType: "a research organization or university that receives NSF research funding"
---

# Query

`awards_by_awardee` returns NSF awards for an organization. Set `awardee` to the
organization name (it is URL-encoded automatically). Read `response.award` (each
has `fundsObligatedAmt`, `title`, `awardeeName`, `date`). Use
`--extract response.award`.

# Matching & caveats

**The `%22` around `{awardee}` in the URL is REQUIRED — do not remove it.**
`awardeeName` performs an exact *phrase* match only when the value is wrapped in
double quotes (`%22`). Unquoted, the API silently falls back to token-OR matching
and returns awards for unrelated organizations that merely share a common word:

| Query | Result |
|---|---|
| `awardeeName=California Institute of Technology` | 25 awards for *University of Massachusetts Boston*, *University of South Dakota*, … — **wrong, and silently so** |
| `awardeeName=%22California Institute of Technology%22` | 25 awards, all *California Institute of Technology* — correct |

This failure is dangerous because it returns a full, plausible-looking result set
rather than an error.

- **Matched by name, not by key.** NSF exposes only `awardeeName`; there is no
  UEI/EIN/identifier to key on. Pass the organization's **canonical full name** —
  abbreviations do not match (`Caltech` → 0 results, `MIT` → matches the unrelated
  *"MIT Development Foundation Inc"*). The harness resolves the mention to its
  canonical label first for exactly this reason; see the `identity` block on
  `research-grants.md`.
- **Paged result.** A request returns at most ~25 award records, so any total is
  over the records returned, not the organization's lifetime NSF funding.
