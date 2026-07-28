---
type: Data Source
title: Grants.gov — Federal Funding Opportunities (access)
description: Open federal grant funding opportunities organizations can apply for, from Grants.gov.
resource: https://api.grants.gov/v1/api/
publisher: grants.gov
trust:
  identity: did:web:grants.gov
  identityType: did
access:
  auth: none
  operations:
    search_opportunities:
      method: POST
      url: "https://api.grants.gov/v1/api/search2"
      body: '{"rows":10,"keyword":"$keyword","oppStatuses":"posted"}'
      capability:
entityType: "US federal grant funding OPPORTUNITIES by topic/program area (not a specific named entity)"
---

# Query

`search_opportunities` returns currently-posted grant opportunities matching a
`keyword`. Read `data.oppHits` (each has `title`, `agency`, `number`,
`closeDate`, `oppStatus`). Use `--extract data.oppHits`.
