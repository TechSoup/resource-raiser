---
type: Data Source
title: USAspending.gov — Federal Awards (access)
description: Federal grants and assistance awards received by an organization, from USAspending.gov.
resource: https://api.usaspending.gov/api/v2/
publisher: usaspending.gov
trust:
  identity: did:web:usaspending.gov
  identityType: did
access:
  auth: none
  operations:
    awards_by_recipient:
      method: POST
      url: "https://api.usaspending.gov/api/v2/search/spending_by_award/"
      body: '{"filters":{"recipient_search_text":["$org"],"award_type_codes":["02","03","04","05"]},"fields":["Award Amount","Recipient Name","Awarding Agency","Award Type","Start Date"],"limit":10,"sort":"Award Amount","order":"desc"}'
      capability:
        identity_field: Recipient Name
        page: {max: 10, complete: false}
    top_recipients:
      method: POST
      url: "https://api.usaspending.gov/api/v2/recipient/?order=desc&sort=amount&limit={n}&page=1"
      body: '{"award_type":"all"}'
      capability:
        group_by: [recipient]
        metrics: [sum(amount)]
        page: {complete: true}
        returns: {path: results, label: name, value: amount}
        grain: recipient
        rows_per_unit: {recipient: 1}
entityType: "an organization that RECEIVES US federal funding — grants or contracts (a nonprofit or company recipient)"
---

# Query

`awards_by_recipient` returns the largest federal grant/assistance awards to an
organization. Set param `org` to the organization name; read `results` (each item
has `Award Amount`, `Recipient Name`, `Awarding Agency`). Use `--extract results`.
Award type codes 02–05 select grants and other financial assistance.

# Matching & caveats

- **Matched by name, not by key.** `recipient_search_text` is a fuzzy name search.
  Because large nonprofits register each local chapter or affiliate as its own
  recipient, one search commonly spans **several separately registered legal
  entities** — e.g. a search for the American Red Cross returns awards belonging
  to the *Southwestern Pennsylvania Chapter*, *Southern Arizona Chapter*, and
  others. A total over these rows is a sum across all of them, **not** the national
  organization's figure. This is declared as `identity.match: name` on
  `federal-awards-received.md`, which makes the harness group the rows by
  `Recipient Name` and state how many distinct recipients matched.
- **A real key exists but is not used yet.** USAspending models recipients with a
  `uei` and a `recipient_level` (`P` parent / `C` child / `R` standalone) via
  `/api/v2/recipient/`. Resolving to a UEI and preferring the parent record would
  let affiliates roll up as a modeled hierarchy instead of a name collision — the
  principled upgrade for this source.
- **Paged result.** The request sets `limit: 10` sorted by award amount descending,
  so a total covers the 10 largest returned awards, not all federal funding
  received.
- Award type codes 02–05 select grants and other financial assistance, so
  **contracts are excluded** from these results.
