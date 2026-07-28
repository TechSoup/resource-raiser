---
type: Nonprofit BMF Fact
title: IRS Ruling Date (Tax-Exempt Since) — IRS Business Master File (Nonprofit)
description: The date the IRS granted a US nonprofit its tax-exempt status (ruling date).
tags:
- nonprofit
- irs
- business-master-file
- charity
- ruling-date
- tax-exempt
- since
source: ./_access.md
bmf: ruling_date
representativeQueries:
- When did this nonprofit become tax-exempt?
- What year did the IRS recognize this charity's exempt status?
- Since when has this organization been a 501(c)(3)?
---

# Schema

Reports the IRS Business Master File `ruling_date` (year and month the IRS
granted tax-exempt status) for a nonprofit keyed by EIN. Resolve the
organization with the `search` operation, then read its ruling date. See
[Nonprofit BMF access](./_access.md).
