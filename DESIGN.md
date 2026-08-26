# Design — how Neural KG works

This document explains the architecture end to end: the one idea it's built on, the request
pipeline, each stage in depth, and the design decisions behind them. For the planner's capability
matrix specifically, see the companion [`DESIGN-query-shapes.md`](DESIGN-query-shapes.md).

**Neural KG is an application of two standards: [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
and [ARD](https://github.com/ards-project/ard-spec).** It contributes no new format and no new
protocol — it is what you get when you put the two together and point them at live data:

- **OKF (Open Knowledge Format)** — Google Cloud's "universal, vendor-neutral format for representing
  knowledge as plain markdown files with YAML frontmatter" (v0.2): bundles of typed, cross-linked
  documents. *Every source here is an OKF bundle.* We add one small extension — an `access:` block —
  that makes a knowledge document **actionable**: it describes not just *what* a dataset is but *how
  to query it*.
- **ARD (Agentic Resource Discovery)** — the protocol for an agent to ask "what resources can serve
  this task?" before acting. *Our Agent Finder is an ARD discovery service* over the OKF bundles.

So: OKF is the noun (knowledge as files), ARD is how an agent finds the right one, and this engine is
the verb (act on the discovered, actionable OKF). Read the
[OKF spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf) (and
[announcement](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing))
and the [ARD spec](https://github.com/ards-project/ard-spec) first — everything below assumes them.

---

## 1. The thesis

Most "chat with your data" systems hard-wire one execution path — *resolve an entity, fetch an
attribute* — and force every question through it. When a question doesn't fit, they don't fail;
they degrade into a name match and return something plausible but wrong ("which university gets the
most NIH funding?" → the 10 largest matching *projects*, presented as a ranking).

Neural KG takes a different stance: **a data source should describe itself well enough that a
generic engine can decide what it can and cannot answer, and route accordingly.** Sources are
described once, declaratively; there is no per-source query code. The engine discovers the right
source, plans against its declared capability, fetches through one generic accessor, and checks the
result before trusting it. This is the ARD (Agentic Resource Discovery) premise: hundreds of small,
well-described resources beat one monolithic schema, because discovery puts only the matching one in
front of the model.

---

## 2. The one abstraction: a source is an OKF document

Everything follows from this, and it is [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
directly. A source lives in `sources/<name>/` as an OKF bundle — plain Markdown files with YAML
frontmatter descriptors — that we make *actionable*:

- `sources/<name>/_access.md` — the source descriptor. Standard OKF frontmatter (`type`, `resource`,
  `tags`, `trust`) plus `entityType` (what it covers), and our **actionable extension**: an
  `access:` block of **operations**. Each operation is a URL template plus a `capability:` block
  declaring what the operation can structurally do. This is the one thing we add to OKF — a
  descriptor of *how to query the resource*, not just what it is.
- `sources/<name>/<table>.md` — one OKF leaf per queryable measure/table ("Apple's revenue concept",
  "median household income", "the grants-made traversal"). A leaf carries the parameters that
  specialize the shared operation (a us-gaap concept, an ACS variable id, a direction marker) and a
  handful of `representativeQueries` used for discovery.

A leaf with no `access:` block inherits its `_access.md` via a `source:` link — an OKF typed
cross-link. Adding a source is adding an OKF folder — no code. Bulk-generated leaf sets (SEC us-gaap,
Census variables) are produced by `tools/gen_*.py` from public taxonomies (OKF's `generated`
descriptor); hand-authored sources (the grant graph, FEMA, …) ship their leaves directly. Because the
sources are standard OKF, any OKF tool can read them, and this engine can act on any actionable OKF
bundle — not just the ones in this repo.

---

## 3. The request pipeline

One question flows through five stages, wrapped in a backtracking search:

```
question
  │
  ├─ discover ──▶ which tables might answer this?            (ARD index: embed + rerank)
  ├─ plan ──────▶ can the chosen source's capability answer  (planner: shape × capability)
  │               this SHAPE of question? refuse if not.
  ├─ fetch ─────▶ fill the OKF operation template, call live (generic accessor)
  ├─ check ─────▶ is the result ABOUT the right thing?       (acceptance test)
  │               if not, backtrack to the next candidate
  └─ synthesize ▶ grounded answer + provenance               (chat model)
```

The whole thing is one depth-first backtracking search (`_solve` in `harness.py`) over the choice
points — candidate table × entity × key/granularity × period — with the acceptance test at the goal.
A dead end backtracks to the next option; the search turns "no data" into "no *wrong* data".

---

## 4. The stages in depth

### 4.1 Discover — the ARD index (`registry/index.py`, `agent_finder.py`)

At build time, `index.py build` embeds every leaf's representative queries into a vector matrix
(`registry/vectors.npy`) with a parallel metadata catalog (`registry/meta.json`). At query time the
Agent Finder (`agent_finder.py`, a small HTTP service) does two stages:

1. **Retrieve** — cosine similarity between the question's embedding and every leaf; take the top
   ~60.
2. **Rerank** — a chat-model pass over those candidates' full cards (title, description, example
   queries), scoring each for relevance. This is a *recall* stage: it keeps close variants and
   siblings rather than committing, because the final choice is made downstream from the actual data.

The classifier (below) also produces a `sources` filter that scopes which source dirs the finder
searches, so a question about philanthropic grants isn't drowned out by federal-award tables.

### 4.2 Plan — query shape × capability (`planner.py`)

Before any network call, the engine classifies the question's **shape** (point, status, entity-list,
comparison, timeseries, ranking, aggregate, filtered-subset, ratio, topical, correlation) and checks
it against the **capability** each candidate source declares. Capabilities are *derived* from the
access grammar where possible (`derive.py` reads key/filter/order/paginate/enumerate off the URL and
body) and declared only for the residue the grammar can't show (grain, page ceilings, whether a scan
is complete).

The planner owns a matrix: `(shape × capability) → EXACT | COMPOSE:<plan> | INFEASIBLE`. A question a
source structurally cannot answer is **refused before a single request**. The load-bearing example:
a source that lists one org's grants can *compare* two named orgs (fan-out, K lookups) but cannot
*rank* the whole population — so a ranking question over it is refused, not faked. Completeness is
scope- and time-scoped: a source may be complete for one org in one fiscal year but not for the
population, so `page.complete_for` and `scope.required` gate this precisely. Full detail:
[`DESIGN-query-shapes.md`](DESIGN-query-shapes.md).

### 4.3 Fetch — the generic accessor (`accessor/okf_fetch.py`, `driver.accessor`)

One fetcher fills the chosen operation's URL (and optional POST body) from params — the leaf's
frontmatter supplies defaults, secrets are pulled from the environment via an `env:NAME` convention
so keys never live in the docs — and performs the request with retry/backoff. Transient failures
(429/5xx, dropped connections) are retried so a flaky endpoint isn't misread as "no data"; a genuine
4xx (e.g. 404 = concept not reported) is a real answer and triggers a backtrack. A non-JSON body (an
API error page, like a keyless Census request's "Missing Key" HTML) is surfaced loudly rather than
parsed as null.

### 4.4 Check + backtrack — the acceptance test (`_answers`, `_solve`)

The result is checked to confirm it's *about* the right thing: the record's measure, unit, currency,
and place/entity must match the question. This is a **routing** check, not a fact-check — it must
never judge the numeric value, because the model's own world-knowledge is wrong about magnitudes,
exchange-rate direction, and recent dates (its training cutoff makes current data look fake). It also
never rejects on a period mismatch: the fetch already backtracks requested → latest, and a period
newer than what's published can't be fetched, so a date rejection would loop forever. On a real
mismatch it backtracks to the next candidate table, entity, granularity, or period, bounded by an
attempt cap so an unsatisfiable question fails cleanly instead of spinning.

### 4.5 Synthesize (`core.py`)

The chat model writes a grounded, concise answer: it quotes the figure and its computed display
strings verbatim, cites the source *named in the data* (not a guess), and reflects declared caveats
(partial coverage, ambiguous measure, cross-source approximation). It is explicitly forbidden from
sanity-checking the value or its date. Different data shapes get different instructions — a ranking
names the leader and never sums across entities; a timeseries reports first/last and the change; an
ambiguous measure is answered per interpretation rather than silently resolved.

---

## 5. The entity spine and the materialized commons (`store.py`, `store_backends.py`)

Cross-source questions need a shared address for a real-world thing. The commons normalizes fetched
records into observations `(entity, measure, period) → value`, keyed on a canonical spine id
(`fips/06001`, `ein/530196605`, `qid/Q180`). Once two measures are materialized on the spine, a join
between them is a local dict intersection — the cross-source cost is paid once, at materialization,
not per question. *When* to materialize is a cost decision driven by blowup (rows transferred ÷ units
in the answer): 1× is free (do it), a large blowup is un-pushable residue done once per vintage.

Storage is a swappable backend chosen by deployment, never hard-coded: sqlite locally, JSON for
inspection, Postgres/BigQuery in the cloud, with a tiered hot/cold (sqlite + object store) option.
Every backend implements the same five methods, so callers never change.

---

## 6. The relational layer — the IRS 990 grant graph (`grants.py`, `sources/irs-grants/`)

Most sources are `entity → attribute → value` (statistical). The grant graph is `entity → relation →
entity` (relational): every 990 filing that makes grants lists them (Schedule I for charities, 990-PF
Part XV for foundations), and each grant is an edge `funder → recipient (amount, purpose, year)`.
`tools/grants_download.py` streams the IRS e-file XML, extracts the edges into a compact table, and
`grants.py` traverses it — forward (who X funds), reverse (who funds X), rankings, geographic flows,
shared-grantee intersections, and by-cause aggregation (joined to the IRS BMF NTEE lookup).

Discovery only has to decide that a question is a grant-graph question; the specific traversal is
chosen deterministically from the question in code, because reranking among seven near-identical
grant leaves is less reliable than a keyword rule. This is the template for the other relational
sources on the roadmap (board membership, corporate ownership, campaign finance): describe the source
for ARD, but pick the traversal in code.

---

## 7. Model provider abstraction (`llm.py`)

Chat (classification, planning, synthesis, acceptance) and embeddings (discovery) go through one
provider-agnostic module. It supports Azure OpenAI, OpenAI, and Gemini — all via the OpenAI SDK,
Gemini through its OpenAI-compatible endpoint — auto-detected from whichever key is set (override
with `LLM_PROVIDER`). The rest of the codebase calls `llm.chat` / `llm.embed` and never knows the
provider. Embedding falls back to one-at-a-time if a provider rejects a batched request.

---

## 8. Key design decisions

- **Describe, don't code — in OKF.** A source is an OKF document, not a module. Building on OKF
  rather than a bespoke format is deliberate: the descriptions are portable and tool-agnostic, and
  making them *actionable* (the `access:` extension) is the smallest addition that turns a knowledge
  file into something an agent can query. This is what makes the source count scale (thousands of
  tables) without the code scaling — and what makes ARD discovery the point.
- **Plan before fetch.** Refusing an infeasible question with zero requests is a feature: a wrong
  ordering is not improved by a footnote. The planner is where correctness is enforced structurally.
- **Route, don't fact-check.** The acceptance test judges *what a record is about*, never its value.
  Letting the model second-guess magnitudes or recent dates causes confident false rejections of
  correct data.
- **Capabilities derived, residue declared.** The access grammar already shows most of what an
  operation can do; only the un-derivable residue (grain, ceilings) is hand-declared, so the docs
  stay honest and small.
- **Materialize on cost, on a spine.** Cross-source joins are paid once, on canonical ids, only when
  the blowup math says so.
- **Deployment picks the backend and the provider.** Storage and LLM are swappable at the edges; the
  core pipeline is unchanged whether it runs on a laptop (sqlite + one API key) or in the cloud.

---

## 9. Extending it

Add `sources/<name>/_access.md` describing the endpoint and one or more operations with a
`capability:` block, plus leaf files (or a `tools/gen_<name>.py` that writes them). Rebuild the index
(`python3 registry/index.py build`) and the source is discoverable and plannable — no other code. For
a relational source, add a query module and a marker on the leaves, and pick the traversal in code as
the grant graph does. See any existing `sources/*/_access.md` and `DESIGN-query-shapes.md`.

---

## 10. References

- **Open Knowledge Format (OKF)** — the format every source here is written in. Spec:
  <https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf> · announcement:
  <https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing>
- **Agentic Resource Discovery (ARD)** — the discovery protocol this engine's finder implements.
  Spec: <https://github.com/ards-project/ard-spec>
- `DESIGN-query-shapes.md` — the planner's query-shape × capability-class matrix, in full.
