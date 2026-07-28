---
type: Federal Funding Dataset
title: NSF Research Grants — NSF Awards
description: National Science Foundation research grant awards made to an awardee organization.
tags: [nonprofit, research, grants, nsf, science, funding]
source: ./_access.md
search:
  operation: awards_by_awardee
  arg: awardee
  want: organization
  extract: response.award
identity:
  match: name          # this source matches recipients by NAME, not a canonical key
  field: awardeeName
representativeQueries:
  - "How much NSF funding does a university receive?"
  - "NSF research awards to an organization"
  - "science research grants funded by the National Science Foundation"
---

# Schema

NSF award records: `fundsObligatedAmt` (USD), `title`, `awardeeName`, `date`,
`startDate`.

# Query

Use operation `awards_by_awardee` with `awardee=<organization name>`; extract
`response.award`. See [NSF Awards access](./_access.md).
