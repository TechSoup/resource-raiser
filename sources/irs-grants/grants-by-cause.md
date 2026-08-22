---
type: Grant Graph — By Cause (thematic)
title: Grant money by cause — IRS 990 grant graph
description: How grant dollars break down by the CAUSE of the recipient — education,
  health, environment, housing, arts, human services, and so on — and how much goes
  to any one cause, by joining recipients to their IRS NTEE classification. IRS 990
  e-file data, 2022-2024.
tags:
- grants
- philanthropy
- cause
- theme
- education
- health
- environment
- what-gets-funded
source: ./_access.md
irsgrants:
  direction: theme
representativeQueries:
- How much grant money goes to education?
- What causes get the most grant funding?
- How much funding goes to health versus education?
- What kinds of causes do grants support?
- How much grant money is directed to the environment?
---

# Schema

Groups grant dollars by the recipient's NTEE major cause group (Education, Health Care,
Environment, …), or totals the money going to one named cause. Joins the recipient EIN on each
grant to the IRS Business Master File NTEE classification. Because only Schedule I edges carry a
recipient EIN, this is the charity-to-charity slice — foundation (990-PF) grants are not
cause-classified. See [the grant graph access doc](./_access.md).
