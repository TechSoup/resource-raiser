# Design: Query Shapes × API Capability Classes

## Problem

The harness has one hard-coded execution shape: *resolve one entity → fetch one
attribute*. Every question is forced through it. When a question doesn't fit, the
pipeline doesn't fail — it **degrades into a name match and returns something
plausible but wrong**:

- *"Which university gets the most from NIH?"* → searched for the literal word
  `university`, returned the 10 largest matching projects, presented as a ranking.
- *"NSF research awards for MIT"* → matched *"MIT Development Foundation Inc"*
  ($107k) instead of MIT ($16M).
- *"How much federal funding has the American Red Cross received?"* → silently
  summed 7 separately registered chapters.

Each was patched individually (`identity.match`, `aggregate.rankable`, coverage
notes). Those patches are three instances of one missing abstraction: **nothing
describes what kind of API a source is, so nothing can decide what kinds of
questions it can answer.**

The fix is two taxonomies and a matrix between them:

1. **Query shape** — what the question asks for structurally.
2. **API capability class** — what the source can structurally do.
3. A **matrix** mapping (shape × capability) → `EXACT` | `COMPOSE` | `INFEASIBLE`.

The doc declares capability. The **engine** owns the matrix. That keeps the
project's premise intact: described once, no per-source code.

---

## 1. Query shapes

| # | Shape | Subject | Returns | Example |
|---|---|---|---|---|
| Q1 | **POINT** | one entity | scalar / record / boolean | "Apple's total revenue"; "Is the Sierra Club a 501(c)(3)?" |
| Q2 | **ENTITY-LIST** | one entity | records about it | "NSF awards for MIT" |
| Q3 | **COMPARISON** | K *named* entities | ordered / diffed | "Does Harvard or MIT get more NIH funding?" |
| Q4 | **TIMESERIES** | one entity, N periods | series | "Apple revenue 2019–2024" |
| Q5 | **RANKING** | open population | top-N ordered | "Which university gets the most NIH funding?" |
| Q6 | **AGGREGATE** | open population | one statistic | "Total NIH funding to all universities"; "How many 501(c)(3)s are there?" |
| Q7 | **FILTERED-SUBSET** | population + predicate | records | "Nonprofits in Chicago with revenue > $1M" |
| Q8 | **TOPICAL** | no entity, keyword | records | "Grants for education" |
| Q9 | **CROSS-SOURCE** | one entity / population, ≥2 sources | combined | "Red Cross revenue vs. federal funding it receives" |

The load-bearing distinction is **Q3 vs Q5**. Both feel like "who is biggest",
but Q3 is over a *closed, named* set (answerable by K lookups against a plain
keyed API) while Q5 is over an *open population* (requires the source to be able
to enumerate or aggregate). Conflating them is what produced the fake NIH
leaderboard.

---

## 2. API capability classes

| # | Class | Can do | Cannot do |
|---|---|---|---|
| C1 | **point** | key → one record | enumerate, rank |
| C2 | **entity-list** | key → many records about that entity | see other entities |
| C3 | **predicate-search** | non-key criteria (keyword/geo/measure/date) → records | order the population |
| C4 | **population-scan** | traverse the population, often ordered, usually with a ceiling | exceed its offset cap |
| C5 | **server-aggregate** | server-side group-by / sum / count / order | — |
| C6 | **bulk** | obtain the entire dataset | answer interactively |

Capabilities are **per operation**, not per source: one source often has several
(ProPublica has a C1 `organization` *and* a C3 `search`).

### Our sources, classified (verified by probe)

| Source | Class | Evidence |
|---|---|---|
| SEC EDGAR `companyconcept` | C1 | key = CIK |
| IRS 990 / BMF `organization` | C1 | key = EIN |
| Wikidata/Wikipedia profile | C1 | key = QID (C5 available via SPARQL, unused) |
| ProPublica `search` | C3 | fuzzy name → org list |
| NSF awards | C2 | `awardeeName` only; ~25 cap |
| NIH RePORTER | C2 + partial C4 | offset ceiling ~15k of 83.5k FY2024 projects |
| Grants.gov | C3 | keyword |
| Treasury FiscalData | C3 + C4 | `sort=-record_date`, full pagination |
| **US Census ACS** | **C4** | `for=county:*&in=state:06` → **all 58 CA counties in one call** |
| **CDC PLACES (Socrata)** | **C4/C5** | `$order=data_value DESC` ranks the whole population server-side |
| **USAspending `/recipient/`** | **C5** | returns recipients pre-ranked by total amount |

**Three sources already support population queries and we never declared it.**
The capability was there; the description wasn't.

---

## 3. The matrix

`EXACT` = one call. `COMPOSE:<plan>` = several calls + client-side computation.
`INFEASIBLE` = refuse before issuing any request.

| Shape ↓ / Capability → | C1 point | C2 entity-list | C3 predicate | C4 pop-scan | C5 aggregate | C6 bulk |
|---|---|---|---|---|---|---|
| **Q1 POINT** | EXACT | EXACT | EXACT | EXACT | EXACT | — |
| **Q2 ENTITY-LIST** | ✗ | EXACT | — | — | — | — |
| **Q3 COMPARISON** | `fan-out-entities` | `fan-out-entities` | — | EXACT | EXACT | — |
| **Q4 TIMESERIES** | `fan-out-periods` | `paginate` | — | EXACT | EXACT | — |
| **Q5 RANKING** | **INFEASIBLE** | **INFEASIBLE** | **INFEASIBLE** | `scan-and-rank` † | EXACT | `bulk-rank` |
| **Q6 AGGREGATE** | **INFEASIBLE** | **INFEASIBLE** | **INFEASIBLE** | `partition-union` † | EXACT | `bulk-agg` |
| **Q7 FILTERED-SUBSET** | ✗ | ✗ | EXACT | `scan-and-filter` | EXACT | — |
| **Q8 TOPICAL** | ✗ | ✗ | EXACT | — | — | — |
| **Q9 CROSS-SOURCE** | `spine-join` | `spine-join` | `spine-join` | `spine-join` | `spine-join` | — |

† **Only if completeness is adequate.** A C4 scan with an offset ceiling below the
population size yields a *biased* answer, not merely a partial one. Aggregating
NIH's top 5,000 projects put Johns Hopkins at $277M against an actual ~$1B,
because summing the largest awards favours institutions with a few huge grants.
A `scan-and-rank` whose coverage is partial must **downgrade to INFEASIBLE**, not
answer with a caveat — a wrong ordering is not improved by a footnote.

### Compose plans

| Plan | Method | Bound |
|---|---|---|
| `fan-out-entities` | resolve each named entity, one read each, compare | K ≤ 8 |
| `fan-out-periods` | one read per period | N ≤ 20 |
| `paginate` | follow pages until exhausted | only if `page.complete` |
| `scan-and-rank` | ordered scan, client-side top-N | needs coverage ≥ threshold |
| `partition-union` | split population so each slice < ceiling, union, aggregate | needs a partition field |
| `spine-join` | resolve entity once on the QID spine, fetch each source, combine | existing `retrieve_for` |

---

## 4. OKF extension

Each **operation** gains a `capability` block. This is the new expressiveness:
the doc states what the API *is*, not just how to call it.

```yaml
access:
  operations:

    # C1 — keyed point read (SEC)
    company_concept:
      method: GET
      url: "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}/us-gaap/{concept}.json"
      capability:
        class: point
        key: {field: cik, kind: canonical-id}     # canonical-id | name

    # C2 — keyed list, name-matched, capped (NSF)
    awards_by_awardee:
      method: GET
      url: "...?awardeeName=%22{awardee}%22&..."
      capability:
        class: entity-list
        key: {field: awardee, kind: name}          # => engine groups + discloses
        identity_field: awardeeName                # where the row states its identity
        page: {max: 25, complete: false}           # => totals are partial
        population: {enumerable: false}            # => Q5/Q6 INFEASIBLE

    # C4 — enumerable population (Census)
    acs:
      method: GET
      url: "https://api.census.gov/data/2022/acs/acs5/profile?get={get}&for={geo}&key={key}"
      capability:
        class: population-scan
        population:
          enumerable: true
          wildcard: "{level}:*"                    # county:* enumerates every county
          partition: "in=state:{fips}"             # scope keeps each slice small
          complete: true
        order: {server: false}                     # rank client-side after enumerating

    # C5 — server-side ranking (USAspending)
    top_recipients:
      method: POST
      url: "https://api.usaspending.gov/api/v2/recipient/"
      capability:
        class: server-aggregate
        group_by: [recipient]
        metrics: [sum(amount)]
        order: {server: true, by: [amount]}
```

Notes:

- `key.kind: name` **subsumes** the ad-hoc `identity:` block added earlier — the
  grouping and "matched N separate recipients" disclosure become derived
  behaviour of a name-keyed capability.
- `page.complete: false` **subsumes** the hand-written coverage caveats.
- `population.enumerable` **subsumes** `aggregate.rankable`.
- Prose `# Matching & caveats` stays. Machine-readable capability says *what*;
  prose says *why and how it fails* (e.g. NSF's `%22` requirement). Both are needed.

---

## 5. Engine: plan before fetch

```
question
  → classify shape            (Q1..Q9)
  → select candidate sources  (existing ARD discovery)
  → read capability from OKF
  → matrix lookup             → EXACT | COMPOSE:<plan> | INFEASIBLE
  → INFEASIBLE?  refuse now, naming what's missing — issue no requests
  → emit PLAN (streamed to the UI)
  → execute plan, carrying a completeness measure
  → completeness below threshold for a superlative? downgrade to refusal
  → synthesize, scoped by completeness
```

Two properties worth preserving:

- **Refusal happens before any HTTP request.** "No point even trying" is a
  planning decision, not a failed fetch.
- **The plan is a first-class artifact.** Streaming *"Plan: fan-out over 2
  entities → 2 keyed reads → compare"* into the console shows the reasoning,
  which is the demo's whole argument.

Backtracking is unchanged; it now operates *within* a chosen plan.

---

## 6. What this changes

**Unlocks (capability that already exists, undeclared):**

- *"Which California county has the highest poverty rate?"* — Census C4, one call, EXACT.
- *"Which city has the highest diabetes rate?"* — CDC C5 `$order`, EXACT.
- *"Which organization receives the most federal funding?"* — USAspending C5, EXACT.
- *"Does Harvard or MIT get more NIH funding?"* — Q3 COMPARISON via `fan-out-entities`;
  works on plain C2 APIs, no new source capability needed.
- *"Apple revenue 2019–2024"* — Q4 via `fan-out-periods`.

**Correctly refuses, before issuing a request:**

- *"Which university gets the most NIH funding?"* — Q5 × C2, and NIH's C4 coverage
  is only ~18% and biased.

**Retires three ad-hoc patches** into one declarative mechanism.

---

## 7. Implementation order

1. **Add `capability` blocks to all `_access.md` files.** Pure data; no behaviour
   change. Generators already preserve `_access.md`.
2. **Shape classifier + matrix + planner.** Replaces the `shape == "ranking"`
   guard with a general lookup. Refuse before fetch; stream the plan.
3. **`fan-out-entities` and `fan-out-periods`.** Cheapest plans, unlock Q3/Q4 on
   sources we already have.
4. **`scan-and-rank` for C4/C5.** Unlocks the Census/CDC/USAspending rankings.
5. **Derive name-match grouping and page-coverage from capability**, deleting the
   `identity:` block and the `aggregate.rankable` hook.
6. **`partition-union`** last — only NIH needs it, and it is best precomputed.

Independent of this: SEC concept selection is unstable across the ~8,000
near-synonym XBRL concepts (Apple's "total revenue" currently returns the
discontinued `us-gaap:Revenues` at FY2018). That is a **discovery-quality**
problem, orthogonal to query shapes, and is not addressed here.

---

# Addendum: the materialization layer (a demand-driven data commons)

## Why it exists

Some shapes need a reduction the API will not perform. The deciding number is
**blowup = rows transferred / units in the answer**:

| Source | Row grain | For 3,000 counties / 50 states | Blowup |
|---|---|---|---|
| Census ACS | one row per county | 3,000 rows | **1×** |
| CDC PLACES | one row per county | 3,000 rows | **1×** |
| NIH RePORTER | one row per **project** | 83,500 rows, 167 requests | **1670×** |

1× means the source cannot reduce further anyway — local costs nothing extra, so
just do it. A large blowup is un-pushable residue: acceptable **once per vintage**
(immutable history), never per question.

The shape says what reduction is *required*; the declared `pushdown` says what the
server can *perform*. What crosses the wire is the residue. Note that `order+limit`
rescues top-N (a prefix suffices) but **never** an aggregate or a correlation, which
need every unit.

## Storage shape: observations, not blobs

`store.py` caches normalized **observations**, deliberately the Data Commons shape:

    (entity, measure, period) -> value + unit + source

`entity` is a canonical spine id (`fips/06001`, `qid/Q180`, `ein/530196605`), and one
OKF leaf plays the role of one StatVar. Consequences:

- The cross-source join is paid **once, at materialization**, on the spine. Sources
  need no shared key of their own — Census emits `state+county`, CDC emits
  `locationid`; both normalize to `fips/06001`, and `store.align()` is then a dict
  intersection.
- The commons **accretes**: any later question touching an already-materialized
  measure is local and free, regardless of which API originally supplied it.
- This is Data Commons built **incrementally and on demand** rather than centrally
  and up front — with the OKF caveats retained, which a flattened cache usually loses.

## New capability declarations

```yaml
capability:
  grain: project                       # what ONE row represents
  entity_kind: fips                    # how to canonicalize it onto the spine
  entity_field: locationid
  can_aggregate_to: []                 # coarser grains the SERVER can roll up to
  pushdown: [select, project, filter]  # order/limit/group absent -> residue is large
  rows_per_unit: {state: 1670, organization: 1800}
  materializable: {partition: org_states, max_slice: 15000, vintage: fiscal_year}
```

`store.estimate()` turns these into rows/requests/blowup **before** any fetch, so an
expensive materialization is a decision rather than a surprise.

## Verified

- `correlation` shape + `compose:materialize-and-correlate`.
- Median household income × diagnosed diabetes, all 58 CA counties: **r = −0.421**,
  n = 58, joined on county FIPS.
- Cold run materializes; a **differently worded** rerun reports `cached: True` for
  both measures and issues zero API calls.
- Each measure is discovered independently — ranking one list for the whole question
  returns two variants of the same measure, or only one side.
- A measure that is jam-suppressed at the requested grain fails by NAME rather than
  as a mystery "0 units matched".

## Open

- **Circularity.** PLACES estimates are modelled from BRFSS using ACS covariates. If
  poverty/income is among them, correlating PLACES against ACS is partly measuring the
  model's own input. Verify against PLACES methodology, then declare
  `provenance.modeled_from: [census-acs]` so the engine can flag it automatically.
- NIH materialization (167 requests) is estimated but not yet wired to a confirmation
  prompt; it would retire ranking, aggregate, filtered-subset and correlation at once.
- Counties are hard for NIH specifically: it partitions by state, not county, so
  county-level totals need an org-address → county geocoding join.

## Storage backends: platform-selected

The commons is relational, so the backend is swappable and chosen by DEPLOYMENT.
For an intermittently-queried service **idle cost dominates**, and materialized
tables are immutable per `(measure, grain, vintage)` — which is what object storage
is good at. So the cheap default on every cloud is a **tiered** backend: SQLite hot
tier for fast lookups and SQL joins, object storage cold tier for durability and
sharing between instances.

| Environment | Backend | Selected by |
|---|---|---|
| local (macOS/Linux) | `sqlite` | default — stdlib, no service |
| AWS | `s3` (tiered) | `AWS_LAMBDA_FUNCTION_NAME` / `AWS_EXECUTION_ENV` + `ARD_BUCKET` |
| GCP | `gcs` (tiered) | `K_SERVICE` / `GOOGLE_CLOUD_PROJECT` + `ARD_BUCKET` |
| Azure | `azure` (tiered) | `WEBSITE_INSTANCE_ID` / `AZURE_STORAGE_*` + `ARD_BUCKET` |
| dev / inspection | `json` | `ARD_STORE=json` |
| testing the tier | `localdir` | `ARD_STORE=localdir` |

Order: explicit `ARD_STORE`, then cloud signals, then sqlite. A cloud detected
*without* `ARD_BUCKET` stays local and says so rather than failing.

### Why object storage rather than managed SQL

| Store | Idle/month | Per GB-month |
|---|---|---|
| S3 / GCS / Azure Blob | **$0.00** | ~$0.02 |
| BigQuery | $0.00 | $0.02 (1 TB/mo query free) |
| DynamoDB on-demand | $0.00 | $0.25/M reads, no joins |
| Cloud SQL / RDS / Azure PG | $12–50 | extra |
| Aurora Serverless v2 (0.5 ACU) | ~$43 | extra |

Measured: CDC diabetes for 2,957 counties is 197 KB raw JSON, **28 KB gzipped (14%)**.
A fully materialized commons of ~500 MB costs **~$0.01/month** on any of the three.
Reach for managed SQL only when multiple writers need transactions.

### Verified

- Write lands in both tiers; deleting the hot tier alone and re-reading **rehydrates
  from the object tier** instead of re-querying the upstream API — which is the
  expensive thing being avoided.
- `align_sql()` joins across measures inside the store (58 county rows), not in Python.
- Cloud adapters are ~20 lines each over one `ObjectAdapter` interface; `localdir`
  gives identical semantics for testing without credentials.
- Unimplemented backends fail with an actionable message naming the schema, not a
  traceback.

## Addendum: SEC concept selection — select by data, not by name

The recurring "Apple total revenue = FY2018 $265B" bug was a DISCOVERY defect, and no
tie-break fixed it because the correct concept never reached the fetch stage:

- For "total revenue", text/embedding similarity ranks the concept literally named
  `Revenues` (and dozens of utility `*OperatingRevenue` concepts) above the actual
  post-ASC-606 concept `RevenueFromContractWithCustomerExcludingAssessedTax`, whose
  name sounds *narrower*.
- The LLM reranker, told to "prefer the headline, not a narrow/legacy/sibling variant",
  then DROPS the correct concept entirely — it reached only embedding rank #34 and was
  pruned before any HTTP fetch.

The fix: **choose the concept from the REPORTED DATA, not from names.**

1. Pull a wide EMBEDDING pool (`rerank=False`, threaded through the discovery seam),
   bypassing the reranker that drops the answer.
2. Fetch what the company actually files for each candidate (most 404 fast).
3. Pick with an LLM that sees each candidate's real *latest year and value*:
   a concept last filed years ago is a discontinued alias; among current concepts,
   "total" means the largest in the family, while "diluted EPS" means that exact
   variant, not the largest.

Verified, no hardcoded concept names: Apple revenue → the ASC-606 concept at FY2025
$416B; Apple diluted vs basic EPS correctly distinguished (7.46 vs 7.49); NVIDIA/Walmart
legitimately keep `Revenues` (still current); Microsoft FY2022 = $198.27B. The timeseries
fix compounds: correct concept per year (Apple FY2019 $260B → FY2023 $383B).

This is the same principle as the rest of the system — decide with evidence (the plan→
execute→CHECK loop), here applied to concept selection instead of answer acceptance.

## Addendum: ambiguous measures → separate answers; recall-first reranking

Two coupled changes, prompted by review:

**(a) The reranker must not prefer "narrower/headline" and drop siblings.** Its old
prompt told it to keep the headline measure "not a narrow, legacy, or sibling variant" —
which is exactly what discarded the correct ASC-606 revenue concept. Stage 2 is now
RECALL-oriented: keep every genuinely relevant candidate including close variants and
alternative definitions (both legacy and current revenue; basic AND diluted EPS). Final
selection happens downstream from the reported data, and a dropped candidate can never be
chosen.

**(b) A genuinely ambiguous measure gets SEPARATE answers, not a silent pick.** "earnings"
= net income / operating income / EBITDA / gross profit; "how big" = revenue / assets /
employees / net income. The classifier flags these and lists the distinct interpretations;
the engine answers EACH and presents them side by side rather than choosing one.

- The interpretations run **in parallel** (ThreadPoolExecutor) with a hard ~55s deadline,
  because some interpretations are genuinely unavailable for the entity (a company has no
  clean employee-count or EBITDA concept) and backtrack for a long time — one slow branch
  must not block the answerable ones. Verified: "How big is Microsoft?" → revenue $281.7B,
  assets $619B, net income $101.8B, employees honestly "unavailable".
- Both HTTP servers are now ThreadingHTTPServer and resolver cache writes are lock-guarded,
  so the parallel fan-out is real rather than serialized at discovery.
- `_say` is lock-guarded so worker threads streaming progress don't interleave SSE frames.
