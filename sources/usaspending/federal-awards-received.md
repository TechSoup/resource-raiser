---
type: Federal Funding Dataset
title: Federal Awards Received — USAspending.gov
description: This measure counts the total federal grants and financial assistance
  awards that an organization has received from US government agencies. It specifically
  describes the funding received by organizations, which can include nonprofits and
  companies, highlighting their role as recipients of federal financial support. This
  measure is distinct from broader categories that may include all types of federal
  spending, as it focuses solely on the awards received, excluding any other forms
  of federal financial interactions such as loans or contracts. The reporting is typically
  done on a per organization basis for each fiscal year.
tags:
- nonprofit
- funding
- grants
- federal
- usaspending
- awards
source: ./_access.md
search:
  operation: awards_by_recipient
  arg: org
  want: organization
  extract: results
identity:
  match: name
  field: Recipient Name
representativeQueries:
- How much federal grant money did the American Red Cross receive?
- What federal awards has a nonprofit gotten?
- Which agencies fund this organization?
---

# Schema

Federal award records: `Award Amount` (USD), `Recipient Name`, `Awarding Agency`,
`Award Type`, `Start Date`.

# Query

Use operation `awards_by_recipient` with `org=<organization name>`; extract
`results`. See [USAspending access](./_access.md).
