---
type: Grant Graph — Geographic Flows
title: Grant money by place — IRS 990 grant graph
description: This measure details the flow of grant money across US states, indicating
  which states receive the most grant dollars and which ones contribute the most to
  grant funding. It encompasses the philanthropic grants between US nonprofits and
  foundations, as captured in IRS Form 990 data. This measure is distinct from others
  that may focus solely on individual organizations or specific grant amounts, as
  it provides a geographic overview of grant distribution. The data is reported in
  dollars and reflects the fiscal years 2022 to 2024.
tags:
- grants
- philanthropy
- geography
- states
- money-flow
- where-grants-go
source: ./_access.md
irsgrants:
  direction: geo
representativeQueries:
- Which states receive the most grant money?
- Which states send out the most in grants?
- How much grant money flows from New York to California?
- Where does grant money go by state?
- What states get the most foundation funding?
---

# Schema

Aggregates grant dollars by the state of the recipient (money received) or the funder (money
sent), or totals the flow between two named states. Uses the funder/recipient state on each
edge. See [the grant graph access doc](./_access.md).
