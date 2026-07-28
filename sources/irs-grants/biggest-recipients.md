---
type: Grant Graph — Biggest Recipients (ranking)
title: The biggest grant recipients — IRS 990 grant graph
description: Rank organizations by the grant money they RECEIVE, or by how many different
  funders back them (an in-degree over the grant graph), from IRS 990 e-file data, 2022-2024.
tags:
- grants
- recipients
- philanthropy
- ranking
- most-funded
- population
- who-funds-whom
source: ./_access.md
irsgrants:
  direction: biggest_recipients
representativeQueries:
- Which organizations receive the most grant money?
- Which nonprofits are funded by the most different foundations?
- Who are the biggest grant recipients?
- Which charities get grants from the most funders?
- Rank organizations by total grants received
---

# Schema

Ranks the RECIPIENT side of the grant graph — by total dollars received, or (for "funded by the
most foundations") by the count of distinct funders, which is the recipient's in-degree. The
mirror image of the top-grantmakers ranking. See [the grant graph access doc](./_access.md).
