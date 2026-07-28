---
type: Grant Graph — Grants Made (forward)
title: Grants made by an organization — IRS 990 grant graph
description: The grants a given nonprofit or foundation MADE — its recipients, the amounts,
  and the total it granted — from IRS 990 e-file data (Schedule I + 990-PF), 2022-2024.
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
- Who are the recipients of grants from the Robert Wood Johnson Foundation?
- What charities does this foundation support?
---

# Schema

Given a funder (named), returns the recipients it granted to, biggest first, with per-recipient
totals and grant counts plus the funder's overall total granted. Forward traversal of the grant
graph (out-edges). The funder is matched by EIN via the nonprofit resolver. See
[the grant graph access doc](./_access.md).
