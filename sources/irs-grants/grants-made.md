---
type: Grant Graph — Grants Made (forward)
title: Grants made by an organization — IRS 990 grant graph
description: This measure captures the grants made by a specific nonprofit or foundation,
  detailing the recipients, the amounts granted, and the total sum of grants issued.
  It pertains to the philanthropic grants between US nonprofits and foundations, as
  reported in IRS Form 990 data. Unlike measures that focus on grants received or
  overall funding trends, this data specifically highlights the outflows of grant
  money from a single organization. The reporting is done per organization for the
  fiscal years 2022 to 2024.
tags:
- grants
- foundation
- philanthropy
- funding
- nonprofit
- who-funds-whom
- grantmaking
source: ./_access.md
irsgrants:
  direction: forward
representativeQueries:
- Who does the Ford Foundation fund?
- What grants did the Gates Foundation make?
- Who does the MacArthur Foundation give money to?
- What organizations does the Mellon Foundation fund?
- List the grants made by the Hewlett Foundation
---

# Schema

Given a funder (named), returns the recipients it granted to, biggest first, with per-recipient
totals and grant counts plus the funder's overall total granted. Forward traversal of the grant
graph (out-edges). The funder is matched by EIN via the nonprofit resolver. See
[the grant graph access doc](./_access.md).
