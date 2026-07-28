---
type: Data Source
title: FEMA OpenFEMA — Disaster Declarations (access)
description: US federal disaster declarations (wildfire, flood, hurricane, severe
  storm) by state, from the FEMA OpenFEMA API. Key-free.
resource: https://www.fema.gov/api/open/v2/
publisher: fema.gov
trust:
  identity: did:web:fema.gov
  identityType: did
access:
  auth: none
  operations:
    declarations:
      method: GET
      url: https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries?$filter=state
        eq '{state}'&$top=50&$orderby=declarationDate desc
entityType: a US state, for federal disaster declarations and emergency assistance
  (e.g. California, Texas, Florida)
---

# About

Federal disaster declarations by US state — wildfires, floods, hurricanes, severe storms — relevant to disaster-relief nonprofits and to community context for grant applications. Keyed by state (a place mention is normalized to a 2-letter code). Each row has `declarationTitle`, `incidentType`, `declarationDate`, `fyDeclared`.
