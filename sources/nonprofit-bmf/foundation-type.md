---
type: Nonprofit BMF Fact
title: Foundation Type — IRS Business Master File (Nonprofit)
description: Whether a US 501(c)(3) is a public charity or a private foundation (and its subtype), decoded from its IRS foundation code.
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
