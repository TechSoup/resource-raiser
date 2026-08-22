---
type: Nonprofit BMF Fact
title: Foundation Type — IRS Business Master File (Nonprofit)
description: This measure identifies whether a US 501(c)(3) organization is classified
  as a public charity or a private foundation, including its specific subtype, based
  on the IRS foundation code. It specifically pertains to the classification of nonprofits,
  distinguishing it from other measures that may focus on financial data or operational
  status. This measure does not provide information on the organization's activities
  or mission, concentrating solely on its foundational type. The reporting is done
  per organization.
tags:
- nonprofit
- irs
- business-master-file
- charity
- foundation
- private-foundation
- public-charity
source: ./_access.md
bmf: foundation
representativeQueries:
- Is this organization a private foundation or a public charity?
- What foundation type is this nonprofit?
- Is this a private foundation?
---

# Schema

Reports the IRS Business Master File `foundation_code`, decoded to plain
language (public charity vs. private foundation and subtype), for a nonprofit
keyed by EIN. Resolve the organization with the `search` operation, then read
its foundation type. See [Nonprofit BMF access](./_access.md).
