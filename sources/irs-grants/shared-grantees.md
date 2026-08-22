---
type: Grant Graph — Shared Grantees (intersection)
title: Organizations funded by two funders — IRS 990 grant graph
description: This measure identifies organizations that receive funding from two specified
  funders, illustrating the overlap in their grantmaking activities. It focuses on
  the intersection of philanthropic grants between US nonprofits and foundations,
  as reported in IRS Form 990 data. This measure is distinct from broader analyses
  of grant funding, as it specifically examines the shared grantees between two funders
  rather than overall funding patterns. The data is reported per pair of funders for
  the fiscal years 2022 to 2024.
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
