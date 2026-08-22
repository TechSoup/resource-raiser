---
type: Nonprofit BMF Fact
title: Headquarters Location — IRS Business Master File (Nonprofit)
description: This measure provides the city and state where a US nonprofit is officially
  registered with the IRS, indicating its headquarters location. It specifically pertains
  to the geographical registration of nonprofits, distinguishing it from other measures
  that may focus on financial or operational aspects. This measure does not include
  information about the organization's activities, mission, or compliance status,
  focusing solely on its physical location. The reporting is done per organization.
tags:
- nonprofit
- irs
- business-master-file
- charity
- location
- headquarters
- city
- state
source: ./_access.md
bmf: location
representativeQueries:
- Where is this nonprofit headquartered?
- What city and state is this charity located in?
- Where is this organization based?
---

# Schema

Reports the IRS Business Master File location (`city`, `state`, `address`,
`zipcode`) for a nonprofit, keyed by EIN. Resolve the organization with the
`search` operation, then read its location. See [Nonprofit BMF access](./_access.md).
