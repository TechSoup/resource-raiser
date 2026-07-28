---
type: Data Source
title: IRS Exempt Organization Business Master File — Nonprofit Registration (access)
description: Org-level IRS registration and classification facts for US tax-exempt organizations (location, NTEE sector, foundation type, contribution deductibility, IRS ruling date), from the Exempt Organization Business Master File via the ProPublica Nonprofit Explorer API. Per-fact entries cross-link here.
resource: https://projects.propublica.org/nonprofits/api/v2/
publisher: propublica.org
trust:
  identity: https://projects.propublica.org/nonprofits/
  identityType: https
access:
  auth: none
  operations:
    search:
      method: GET
      url: "https://projects.propublica.org/nonprofits/api/v2/search.json?q={q}"
      capability:
    organization:
      method: GET
      url: "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
      capability:
entityType: "a US nonprofit's IRS REGISTRATION & STATUS facts — 501(c)(3) status, subsection, tax-deductibility of donations, IRS ruling/tax-exempt date, NTEE sector/classification, and whether it is a public charity or a private foundation (NOT its finances or its mission)"
---

# About

The IRS **Business Master File (BMF)** is the registration record for every
US tax-exempt organization — distinct from its annual Form 990 *financial*
filings. It carries where the org is, what sector it works in (NTEE), whether it
is a public charity or a private foundation, whether donations are deductible,
and the date the IRS granted tax-exempt status. The key is the **EIN**.

# Query

1. `search` with `q=<organization name>` → take the top `ein`.
2. `organization` with `ein=<ein>` → the top-level `organization` object carries
   the BMF fields (`city`, `state`, `ntee_code`, `foundation_code`,
   `deductibility_code`, `ruling_date`, …). Each per-fact entry names the field
   and decodes any IRS code to plain language.

# Matching & caveats

- **These are registration facts, not financial ones.** The BMF describes how the
  IRS classifies an organization. For revenue, expenses, or assets use the annual
  Form 990 filings instead — see [Nonprofit 990 access](../nonprofit-990/_access.md).
- **Raw values are numeric codes and are meaningless unquoted.** `foundation_code`,
  `deductibility_code`, and `ntee_code` must be decoded (e.g. foundation code `04`
  = private non-operating foundation; deductibility `2` = contributions are *not*
  deductible). Emitting the raw code, or guessing at it, produces confidently
  wrong answers.
- **Only the NTEE major group (first letter) is decoded here.** The full code
  carries a finer subcategory that this source does not expand.
- **The BMF address is the registered mailing address**, which for some
  organizations is an accountant or agent rather than an operating headquarters.
- `ruling_date` is formatted `YYYYMM`; it is the date exemption was *granted*,
  which may be much later than the organization's founding.
- Resolution shares the fuzzy name `search` of the 990 source, so the same
  chapter-vs-national ambiguity applies — verify the returned `name`.
