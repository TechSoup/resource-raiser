---
type: Federal Funding Dataset
title: Federal Funding Opportunities — Grants.gov
description: This measure reports on open federal grant funding opportunities available
  for organizations to apply for, categorized by various topics or program areas.
  It specifically describes the landscape of federal funding options, allowing users
  to search for grants that align with their interests or needs. This measure is distinct
  from other funding reports as it focuses solely on current opportunities rather
  than historical data or completed grants. The reporting is organized per opportunity,
  providing a comprehensive view of available funding options.
tags:
- nonprofit
- funding
- grants
- opportunities
- grants-gov
- apply
source: ./_access.md
search:
  operation: search_opportunities
  arg: keyword
  want: keyword
  extract: data.oppHits
representativeQueries:
- What federal grants can a nonprofit apply for in education?
- Open grant opportunities for health programs
- Find funding opportunities for the arts
---

# Schema

Open opportunity records: `title`, `agency`, `number`, `closeDate`, `oppStatus`.

# Query

Use operation `search_opportunities` with `keyword=<topic or program area>`;
extract `data.oppHits`. See [Grants.gov access](./_access.md).
