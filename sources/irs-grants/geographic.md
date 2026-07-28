---
type: Grant Graph — Geographic Flows
title: Grant money by place — IRS 990 grant graph
description: Where grant money goes and comes from — which US states receive the most grant
  dollars, which send the most, and how much flows from one state to another, from IRS 990
  e-file data, 2022-2024.
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
