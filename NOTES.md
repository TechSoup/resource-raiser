# Engineering notes and plan

Written for whoever picks up the next round of work. Part 1 is what to build. Part 2 is what this
codebase has already taught us the expensive way — none of it is visible in the code, and all of it
costs a day to rediscover.

---

## Part 1 — The plan

### 0. Operational fixes

Live bugs, independent of everything below.

| | |
|---|---|
| `/healthz` spends credits | it calls `ard_client.search()`, which embeds the query (`registry/index.py`, `search()`) and increments the finder's 500/day quota. A 30s load-balancer probe exhausts it in hours, after which health checks 503 and restart loops follow. Add a finder endpoint that only verifies the loaded index. **Delete the comment above it claiming it makes no LLM call** — that comment is why the bug survived review. |
| `BIND_HOST` exposes the finder | both processes read the same variable, so the documented "expose only the harness" configuration also publishes an unauthenticated, credit-spending `/search`. Split into `HARNESS_BIND_HOST` / `AGENT_FINDER_BIND_HOST`. |
| Streaming did not stream (resolved) | The ASGI path now runs one owned async query task and drains its query-local bounded progress queue into SSE frames while provider calls are in flight. |
| Finder input is unvalidated | `Content-Length`, JSON body, nested `query`, and `pageSize` are parsed without checks, and body size is unbounded. It is a public endpoint now. Return 400/413; clamp `pageSize`. |
| The index is never refreshed | `run.sh` builds only when `vectors.npy` is absent, so a descriptor change never reaches discovery. |

Index publishing should be a versioned generation, not two independent renames — `vectors.npy` and
`meta.json` can skew:

```
regenerate leaves → content-hash manifest → incremental embed → validate dims/counts
→ registry/builds/<hash>/{vectors.npy,meta.json,manifest.json}
→ atomically swap registry/current → restart finder → readiness check
```

The manifest should record: descriptor corpus hash, embedding provider **and** model, vector
dimension, entry count, generator commit, created-at. Generators run at release time, not on every
boot — running them at startup would put the FASB and Census taxonomies on the service's boot path.

### 1. First slice

These are one unit. The fixture boundary makes connectors a prerequisite for the tests.

- **Connectors** — `capabilities / validate / resolve / execute → Evidence`. Generic OKF/REST plus
  SEC, BigQuery and grants. This is also what shrinks `harness.py` (~2,600 lines holding prompts,
  routing, execution, UI, protocol and HTTP).
- **Domain records** — `QueryIntent`, `Attempt[]`, `Validation`, `Evidence`, `Answer`. Keep
  `Attempt` (candidate, outcome, reason) distinct from `Evidence` (facts + provenance); a failed
  concept or suppressed value is trace, not evidence.
- **Structural validation ladder** — entity → measure → unit/currency → period → grain → sentinel →
  scope → source-specific rules → LLM **only** for residual semantic ambiguity. The ladder is
  central and shared; connectors supply the predicates. The model never overrides a deterministic
  failure. Worth measuring whether the LLM step survives at all for point lookups once the
  structural checks land.
- **Golden fixtures** recorded at `connector.execute()`, asserting interpretation and evidence
  rather than live values, and **including the refusals** — the planner declining a ranking over a
  keyed source is the most distinctive behaviour here and the easiest to silently lose.

### 2. Then

- **Deterministic renderers** for point, status, ranking, threshold subset, timeseries and the grant
  directions. Dispatch on **validated evidence type, never on `shape`** — see the overview bug in
  Part 2.
- **Capability fitness in candidate ordering**, lexicographic: discard incapable → prefer exact
  grain and population fit → order the remainder by semantic score. No weighted coefficients; we
  have no calibration for them.
- **Compatibility fields** (`unit`, `currency`, `grain`, `period_basis`, `entity_key`,
  `quantity_kind`, population coverage) used to establish **incompatibility**. Never equivalence.
- **Service controls** — bounded requests, concurrency limits, cancellation/deadlines, graceful
  shutdown, and persisting the per-request cost/latency/attempt telemetry that is already computed
  and then thrown away.
- **UI as analytical debugger** — interpretation, chosen plan, rejected candidates and why,
  evidence, editable assumptions. For a showcase this *is* the argument.

### Explicitly not doing

Multi-stage plan IR (`IntentPlan → SourcePlan → ExecutionPlan`); plan enumeration replacing
empirical backtracking; a canonical measure-equivalence vocabulary; an ontology up front; weighted
capability scores; in-process index hot reload; the package restructure as its own task; replacing
prose descriptions with schemas; distributed quotas without a distributed deployment; authentication
(settled product decision — `/ask` is deliberately public).

---

## Part 2 — Field notes

### Why empirical backtracking cannot be replaced by planning

Capability metadata answers "can this source perform this operation". Only execution answers "does
it hold a value for this entity, period and measure". All three of these happened in one week:

- **SEC returns 404** for `us-gaap:AssetsNet` on Tesla. The concept exists in the taxonomy; the
  company does not report it. `driver.fetch_metric` tries ranked concepts until one has data.
- **Census returns `-888888888`** — a suppression sentinel, not an error, not a value. `_quirk_acs_pe`
  recovers by switching to the percent variant (`DP03_0128E` → `DP03_0128PE`).
- **ProPublica resolves a name to an EIN with no grant edges**, so the reverse lookup falls back to
  a name match and reports which it used.

No descriptor predicts any of these. Enumerate and order candidates; keep the retry.

### Postgres / SQLite dialect traps (already paid for)

The grant graph moved from SQLite to managed Postgres. Every one of these was a silent failure
first:

- **`LIKE` case sensitivity.** SQLite's `LIKE` ignores ASCII case; Postgres's does not. **11.4% of
  recipient names and 11.6% of funder names are not upper-case**, and the queries match an
  upper-cased needle. A literal port drops about one match in nine and reads as "no grants found".
  Use `ILIKE`.
- **Trigram index expression must match the predicate.** An index on `upper(name) gin_trgm_ops`
  cannot serve `name ILIKE '%X%'` — the planner seq-scans 7.8M rows and the query times out at 100s
  *despite the index existing*. Index the column.
- **`pg_trgm` is allow-listed per server on Azure** (`az postgres flexible-server parameter set
  --name azure.extensions --value pg_trgm`) — otherwise `CREATE EXTENSION` fails after the load.
- **SQLite's lax `GROUP BY`.** Selecting a bare column alongside an aggregate is legal in SQLite and
  rejected by Postgres. Fixed with explicit `MAX()`, valid in both.
- **No `ATTACH DATABASE`.** The NTEE lookup is a second SQLite file; in Postgres it is a table in
  the same database, so only the table name differs.
- **`SUM()` over a bigint returns `Decimal`**, which is not JSON-serializable and surfaces as a 500
  at response encoding, far from the query that caused it.

### Performance shape of the grant graph

A 1-vCore Burstable server cannot scan 7.8M rows interactively: the by-cause join took **283s**
live, and the obvious filter only got it to 259s. The table is immutable (2022–2024 filings), so
population-scale aggregates are precomputed into `agg_*` rollups — **283s → ~6ms**. That, plus
per-thread connection pooling, is what makes the cheapest SKU viable. Rebuild with
`tools/grants_to_postgres.py --rollups-only` after any reload.

Connection pooling matters more than it looks: opening a connection per query cost ~1.5s of
cross-continent TLS handshake. After pooling, repeat queries hit exactly one round trip.

### Discovery tuning is counter-intuitive

Measured on the 193-case corpus (`tests/route_eval.py`), smaller prefilter is **both cheaper and
more accurate** — it is not a trade-off:

| prefilter | top-1 | top-3 | $/question |
|---|---|---|---|
| 60 | 91.2% | 93.3% | $0.00096 |
| 40 | 92.2% | 93.3% | $0.00080 |
| 25 | 92.2% | 93.8% | $0.00061 |
| **15** (current default) | **93.8%** | 93.8% | **$0.00051** |
| no re-rank | 89.1% | 93.3% | $0 |

Handing the re-ranker 60 candidates gives it 45 more chances to prefer a plausible sibling. The
re-rank still earns its keep (+4.7pt over none) — it just wants a short list. Re-measure before
changing this.

Related: the re-rank card deliberately **omits the description**. The prefilter has already used it;
repeating it doubled the prompt (838 → 429 chars per card × 60 cards) for no accuracy gain.
`ARD_RERANK_DESC=1` puts it back.

### Known correctness hazards

- **`shape` can be wrong.** "Give me an overview of the grant graph" classified fine but routed to
  the ranking path and reported Fidelity's $39B as the whole-graph total. `_grant_direction` matched
  `"overall"` but not `"overview"`. **This is why renderers must dispatch on validated evidence
  type, not on `shape`.**
- **The classifier is flaky on grant vocabulary.** Identical code, model and prompt routed
  "grant dollars by state" to `usaspending` on the VM and `irs-grants` locally. Three sources share
  the word "grant". `_ensure_grant_graph()` is a deterministic guard that *widens* the candidate
  pool rather than overriding the classifier; federal questions still route federally.
- **Citation can drift from the concept that answered.** `_s_concept` passes the attribute string to
  `driver.fetch_metric`, which re-discovers the concept — so a leaf that 404s can be the ranked hit
  while the number comes from another concept. `_cite_concept_actually_used()` re-cites from the
  data. Any refactor of the SEC path must preserve that.

### Descriptor verification

- **`tools/descriptions_input.json` is the verification baseline** — the exact text each description
  was expanded from. Most generated leaves are gitignored, so there is no committed "before" to diff
  against; without the side-car, a checker validates descriptions against nothing.
- **2,750 leaves have no baseline** (SEC concepts whose FASB definitions already exceeded the
  threshold and were kept verbatim). They are not verifiable by this method and are correctly
  skipped rather than passed.
- **Do not trust the previously reported 0.62% unsupported rate.** It came from an adjudicated run
  whose report went to `/tmp` and whose descriptor state has since changed. At commit `19b8347` the
  deterministic screen reports 1,101 of 6,135 flagged (17.9%); the adjudicated number needs
  re-measuring. **Commit eval reports as artifacts** recording command, commit, provider, model,
  **prompt version** (every description here is prompt-derived, and the prompt changed three times
  during generation — model and commit alone would not distinguish those runs), corpus hash, and
  whether the run was live or replayed.

### Deployment

- **Partial deploys are silent.** An `rsync` failed on a bad flag once and left the VM serving stale
  descriptors against a freshly synced index. Verify both sides after deploying — md5 the modules.
- Services are `rr-finder` and `rr-harness` (systemd, `Restart=always`, logs in
  `/var/log/resource-raiser/`), behind nginx: NLWeb at `/`, Resource Raiser at `/rr/`, ARD API at
  `/ard-api/`, manifest at `/.well-known/ard.json`.
- **The nginx well-known location must stay an exact match.** A prefix match on `/.well-known/`
  would also capture `/.well-known/acme-challenge/` and silently break certificate renewal ~90 days
  later.
- **Never write nginx backups inside `sites-enabled/`** — nginx globs that directory and the backup
  loads as a duplicate server block.
- The finder holds a 54MB in-memory index with **no reload path**; an index refresh requires a
  restart. That interacts directly with the index-publishing work above.

### Costs, for calibration

Per question: ~3–5 chat calls in the harness (~$0.0004), plus discovery in the finder, reported
separately because it is its own service. Classification and synthesis dominate; entity and measure
resolution are ~$0.00007 combined and are cached per process, so a warm repeat is measurably cheaper
than a cold first ask. `GET /costs` reports running totals; `usage` and `discovery_usage` ride on
every answer.
