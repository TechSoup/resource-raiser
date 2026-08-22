---
type: Nonprofit BMF Fact
title: Contribution Deductibility — IRS Business Master File (Nonprofit)
description: This measure reports whether donations made to a US nonprofit organization
  are eligible for tax deductions, based on the IRS deductibility code assigned to
  the organization. It specifically pertains to the tax-deductibility status of contributions
  to nonprofits, distinguishing it from other financial metrics or operational statuses.
  This measure does not provide information on the amount of donations or the overall
  financial health of the organization, focusing solely on the deductibility aspect.
  The reporting is done per organization.
tags:
- nonprofit
- irs
- business-master-file
- charity
- donation
- deductible
- tax
source: ./_access.md
bmf: deductibility
representativeQueries:
- Are donations to this nonprofit tax-deductible?
- Can I deduct a contribution to this charity?
- Is a gift to this organization tax-deductible?
---

# Schema

Reports the IRS Business Master File `deductibility_code`, decoded to plain
language, for a nonprofit keyed by EIN. Resolve the organization with the
`search` operation, then read its deductibility status. See [Nonprofit BMF access](./_access.md).
