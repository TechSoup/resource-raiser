---
type: Federal Funding Dataset
title: NIH Research Grants — NIH RePORTER
description: This measure counts the NIH-funded biomedical research grants and projects
  that are awarded to a research organization or university. It specifically describes
  the financial support provided by the NIH for research activities within these institutions.
  Unlike measures that may focus on individual researchers or specific types of funding,
  this metric encompasses all grants awarded to the organization as a whole. The reporting
  unit is per organization per fiscal year, reflecting the total number of grants
  and projects funded.
tags:
- nonprofit
- research
- grants
- nih
- biomedical
- funding
source: ./_access.md
offset: 0
fy: 2024
search:
  operation: projects_by_org
  arg: org
  want: organization
  extract: results
identity:
  match: name
  field: organization.org_name
representativeQueries:
- How much NIH research funding does Stanford get?
- NIH grants awarded to a research institution
- biomedical research projects funded at a university
---

# Schema

NIH project records: `award_amount` (USD), `project_title`, `organization`,
`fiscal_year`.

# Query

Use operation `projects_by_org` with `org=<organization name>`; extract
`results`. See [NIH RePORTER access](./_access.md).
