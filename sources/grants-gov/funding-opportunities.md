---
type: Federal Funding Dataset
title: Federal Funding Opportunities — Grants.gov
description: Open federal grant opportunities an organization can apply for, searchable by topic or program area.
tags: [nonprofit, funding, grants, opportunities, grants-gov, apply]
source: ./_access.md
search:
  operation: search_opportunities
  arg: keyword
  want: keyword
  extract: data.oppHits
representativeQueries:
  - "What federal grants can a nonprofit apply for in education?"
  - "Open grant opportunities for health programs"
  - "Find funding opportunities for the arts"
---

# Schema

Open opportunity records: `title`, `agency`, `number`, `closeDate`, `oppStatus`.

# Query

Use operation `search_opportunities` with `keyword=<topic or program area>`;
extract `data.oppHits`. See [Grants.gov access](./_access.md).
