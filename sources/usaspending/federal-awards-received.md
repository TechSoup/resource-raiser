---
type: Federal Funding Dataset
title: Federal Awards Received — USAspending.gov
description: Federal grants and financial-assistance awards an organization has received from US government agencies.
tags: [nonprofit, funding, grants, federal, usaspending, awards]
source: ./_access.md
search:
  operation: awards_by_recipient
  arg: org
  want: organization
  extract: results
identity:
  match: name          # this source matches recipients by NAME, not a canonical key
  field: Recipient Name
representativeQueries:
  - "How much federal grant money did the American Red Cross receive?"
  - "What federal awards has a nonprofit gotten?"
  - "Which agencies fund this organization?"
---

# Schema

Federal award records: `Award Amount` (USD), `Recipient Name`, `Awarding Agency`,
`Award Type`, `Start Date`.

# Query

Use operation `awards_by_recipient` with `org=<organization name>`; extract
`results`. See [USAspending access](./_access.md).
