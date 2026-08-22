---
type: Grant Graph — Grants Received (reverse)
title: Who funds an organization — IRS 990 grant graph
description: The funders that gave grants TO a given organization (a school, church,
  nonprofit) — the charities and foundations behind it, the amounts, and the total
  it received — from IRS 990 e-file data (Schedule I + 990-PF), 2022-2024.
tags:
- grants
- funders
- philanthropy
- funding
- nonprofit
- who-funds-whom
- donors
source: ./_access.md
irsgrants:
  direction: reverse
representativeQueries:
- Which foundations fund Stanford?
- Who funds the American Red Cross?
- What charities have funded this university?
- Which foundations give money to Feeding America?
- Who are the funders of this organization?
---

# Schema

Given a recipient (named), returns the funders that granted to it, biggest first, with per-funder
totals and grant counts plus the recipient's overall total received. Reverse traversal of the grant
graph (in-edges). The recipient is matched by EIN (clean, from Schedule I) and by name (needed for
990-PF, which carries no recipient EIN); the result states which. See
[the grant graph access doc](./_access.md).
