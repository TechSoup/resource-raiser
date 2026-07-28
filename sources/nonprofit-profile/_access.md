---
type: Data Source
title: Organization Profile — Wikidata & Wikipedia (access)
description: Descriptive, non-financial profile facts about a nonprofit — year founded, headquarters, employee count, official website, leadership, founders, and a plain-English overview — from Wikidata and Wikipedia. Key-free and live. Per-fact entries cross-link here.
resource: https://www.wikidata.org/
publisher: wikidata.org
trust:
  identity: did:web:wikidata.org
  identityType: did
access:
  auth: none
  operations:
    entity:
      method: GET
      url: "https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qid}&props=claims|labels|sitelinks&format=json"
      capability:
entityType: "a US nonprofit's DESCRIPTIVE profile — its mission and what it does, leadership (CEO/founder), year founded, headquarters location, website, and employee count (from Wikidata/Wikipedia; NOT its finances or its IRS registration facts)"
---

# About

Beyond finances, an organization has a **profile**: when it was founded, where
it is based, how many people it employs, who leads it, and what it does. These
facts come from **Wikidata** (structured claims) and **Wikipedia** (the lead
overview), keyed by the entity's Wikidata QID — which the resolver already
assigns on the cross-source spine, so no extra lookup is needed.

# Query

Each per-fact entry names a Wikidata property (or the Wikipedia overview). Given
the resolved QID, fetch `wbgetentities` and read the property; entity-valued
properties (headquarters, chief executive, founder) are resolved to their English
labels. The overview is the Wikipedia REST `summary` extract for the QID's
`enwiki` sitelink.

# Matching & caveats

- **Coverage is uneven and absence is normal.** Wikidata is community-curated, so
  many legitimate nonprofits carry no employee count, leader, or founder. A
  missing property is a genuine "not recorded", not a lookup failure — the caller
  should fall back to another source rather than retrying.
- **Currency is not guaranteed.** Unlike the IRS sources, nothing forces Wikidata
  to be up to date; a leadership claim in particular can name a predecessor long
  after they left. Attribute these facts to Wikidata explicitly rather than
  stating them as current fact.
- **A property can hold many values across time.** Leadership (`P169`) commonly
  lists every past holder. Select the *current* one by taking the claim with no
  end-time qualifier (`P582`); using the first claim returns whoever happens to
  sort first, usually a former holder.
- **Entity-valued properties return QIDs, not text.** Headquarters, leader, and
  founder come back as `Q…` ids needing a second label lookup, and some items
  have no English label — drop those rather than emitting a raw QID as an answer.
- This source is keyed by **Wikidata QID**, not EIN. It is the only nonprofit
  source here not keyed by the IRS identifier, so it joins on the resolver's
  cross-source spine instead.
