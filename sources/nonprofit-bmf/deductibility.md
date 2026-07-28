---
type: Nonprofit BMF Fact
title: Contribution Deductibility — IRS Business Master File (Nonprofit)
description: Whether donations to a US nonprofit are tax-deductible, decoded from its IRS deductibility code.
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
