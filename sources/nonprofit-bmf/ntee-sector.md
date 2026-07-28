---
type: Nonprofit BMF Fact
title: NTEE Sector / Classification — IRS Business Master File (Nonprofit)
description: The NTEE mission sector a US nonprofit works in (e.g. Human Services, Education, Health Care), decoded from its IRS NTEE code.
tags:
- nonprofit
- irs
- business-master-file
- charity
- ntee
- sector
- classification
- mission
source: ./_access.md
bmf: ntee
representativeQueries:
- What sector or field does this nonprofit work in?
- What is this charity's NTEE classification?
- Is this organization an arts, education, health, or human services nonprofit?
---

# Schema

Reports the IRS Business Master File `ntee_code`, decoded to its NTEE major
group (mission sector), for a nonprofit keyed by EIN. Resolve the organization
with the `search` operation, then read its sector. See [Nonprofit BMF access](./_access.md).
