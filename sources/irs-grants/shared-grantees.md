---
type: Grant Graph — Shared Grantees (intersection)
title: Organizations funded by two funders — IRS 990 grant graph
description: Given TWO named funders, the organizations they BOTH fund — a grant-graph
  intersection showing where two foundations' giving overlaps, from IRS 990 e-file data,
  2022-2024.
tags:
- grants
- philanthropy
- co-funding
- overlap
- shared-grantees
- graph-pattern
- foundations
source: ./_access.md
irsgrants:
  direction: shared
representativeQueries:
- Do the Gates and Ford foundations fund any of the same organizations?
- Which organizations do the Mellon and MacArthur foundations both fund?
- What grantees do these two foundations have in common?
- Which nonprofits get money from both of these funders?
- Where does the giving of two foundations overlap?
---

# Schema

Given two named funders, returns the organizations that appear as recipients of BOTH — the
intersection of their out-edges — with each funder's amount. A relational query the per-org
APIs cannot express. See [the grant graph access doc](./_access.md).
