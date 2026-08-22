---
type: Federal Funding Dataset
title: NSF Research Grants — NSF Awards
description: This measure counts the total number of research grants awarded by the
  National Science Foundation to various research organizations or universities. It
  specifically describes the funding received by these entities for research purposes,
  distinguishing it from other types of awards such as fellowships or contracts. The
  measure does not include non-research funding or grants awarded to individuals.
  The reporting unit is the count of grants awarded per organization per fiscal year.
tags:
- nonprofit
- research
- grants
- nsf
- science
- funding
source: ./_access.md
search:
  operation: awards_by_awardee
  arg: awardee
  want: organization
  extract: response.award
identity:
  match: name
  field: awardeeName
representativeQueries:
- How much NSF funding does a university receive?
- NSF research awards to an organization
- science research grants funded by the National Science Foundation
---

# Schema

NSF award records: `fundsObligatedAmt` (USD), `title`, `awardeeName`, `date`,
`startDate`.

# Query

Use operation `awards_by_awardee` with `awardee=<organization name>`; extract
`response.award`. See [NSF Awards access](./_access.md).
