---
type: Nonprofit BMF Fact
title: Headquarters Location — IRS Business Master File (Nonprofit)
description: The city and state where a US nonprofit is registered with the IRS.
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
