---
type: Nonprofit BMF Fact
title: NTEE Sector / Classification — IRS Business Master File (Nonprofit)
description: This measure categorizes the NTEE mission sector in which a US nonprofit
  operates, such as Human Services, Education, or Health Care, based on the IRS NTEE
  code. It specifically pertains to the mission classification of nonprofits, distinguishing
  it from financial metrics or compliance statuses. This measure does not provide
  information on the organization's operational effectiveness or financial health,
  focusing solely on its mission sector. The reporting is done per organization.
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
