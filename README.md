# Resource Raiser

An agentic natural-language data-query engine that demonstrates a simple thesis:
**data is the most important agentic resource, and it should be discoverable via
[ARD](https://github.com/ards-project/ard-spec) (Agentic Resource Discovery).**

You ask a plain-English question. An in-memory ARD index discovers which dataset can answer it.
A planner decides whether the question is even answerable by that source *before* touching the
network. A single generic accessor fetches the data live. The answer is synthesized with
provenance and checked against the question — and if the wrong table was picked, the search
backtracks to the next candidate.

There is **no per-source query code**. Every source is described once, as an *actionable* document
in the [Open Knowledge Format (OKF)](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf),
and the engine does the rest. This project is essentially OKF made queryable: OKF represents
knowledge as plain markdown-with-frontmatter files, and we add a small `access:` extension that
describes *how to query* each source, then discover and act on them via ARD. The demo covers ~20
authoritative US data sources — SEC, Census, Treasury, IRS Form 990, CDC, federal grants, and more —
plus the **IRS 990 grant graph**: who funds whom across every US nonprofit and foundation, queryable
in both directions.

---

## Table of contents

- [Quickstart](#quickstart)
- [Choosing a model provider](#choosing-a-model-provider)
- [What the first run builds](#what-the-first-run-builds)
- [How it works](#how-it-works)
- [The IRS 990 grant graph](#the-irs-990-grant-graph)
- [Data sources](#data-sources)
- [Using it](#using-it)
- [Adding a source](#adding-a-source)
- [Configuration reference](#configuration-reference)
- [Repository layout](#repository-layout)
- [Troubleshooting](#troubleshooting)

---

## Quickstart

```bash
git clone https://github.com/TechSoup/resource-raiser.git
cd resource-raiser

pip install -r requirements.txt            # numpy, pyyaml, openai (+ optional google-cloud-bigquery)

cp set_keys.example.sh set_keys.sh         # then fill in ONE provider's keys — see SETUP.md
./run.sh
```

Full per-provider setup — **OpenAI, Gemini, Azure, or local Ollama**, with model ids and the
free-tier/local gotchas — is in **[SETUP.md](SETUP.md)**.

The **first `./run.sh` takes ~10 minutes** (see [below](#what-the-first-run-builds)); every run
after that starts in a few seconds. When it's up, open **http://127.0.0.1:8099/** and try:

- *"What is the poverty rate in Chicago?"*
- *"Is the Sierra Club a 501(c)(3)?"*
- *"Which foundations fund Stanford?"*
- *"Apple's total revenue"*

Requires Python 3.9+.

---

## Choosing a model provider

The engine uses a chat model (for classification, planning, synthesis, and the acceptance check)
and an embedding model (for ARD discovery). It works with **Azure OpenAI, OpenAI, or Gemini** —
pick whichever you have. The provider is auto-detected from whichever key you set; force it with
`LLM_PROVIDER=azure|openai|gemini`.

| Provider | Set these | Chat / embed defaults |
|---|---|---|
| **OpenAI** | `OPENAI_API_KEY` | `gpt-4o-mini` / `text-embedding-3-large` |
| **Gemini** | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | `gemini-2.0-flash` / `text-embedding-004` |
| **Azure OpenAI** | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`, `CHAT_DEPLOYMENT`, `EMBED_DEPLOYMENT` | (your deployment names) |

All three run through the OpenAI SDK — Gemini via its OpenAI-compatible endpoint — so the rest of
the code is provider-agnostic (see [`llm.py`](llm.py)). Override the model with `CHAT_MODEL` /
`EMBED_MODEL` (OpenAI, Gemini) or the `*_DEPLOYMENT` vars (Azure). Full details in
[`set_keys.example.sh`](set_keys.example.sh).

---

## What the first run builds

The repo ships the **code, the generators, and each source's `_access.md` description** — but not
the ~8,900 machine-generated per-table descriptors, which are rebuilt locally so the repo stays
small and self-describing. On first launch only, `run.sh` runs the generators, then embeds the
ARD index:

```
tools/gen_sec_okf.py       # SEC EDGAR us-gaap concepts, from the FASB taxonomy (~8,100 tables)
tools/gen_census_okf.py    # Census ACS Data Profile variables (~510)
tools/gen_treasury_okf.py  # Treasury FiscalData series (~180)
tools/gen_cdc_okf.py       # CDC PLACES measures (~40)
tools/gen_np_okf.py        # IRS 990 nonprofit fields (~57)
        ↓
registry/index.py build    # embed every table's representative queries -> registry/vectors.npy + meta.json
```

The generators pull from public taxonomies/APIs (no keys needed); the index build calls your
embedding model, which is the bulk of the ~10 minutes. The results are cached, so subsequent runs
skip the whole step. To rebuild manually at any time: `python3 registry/index.py build`.

---

## How it works

The pipeline for one question is **discover → plan → fetch → check → synthesize**, with
backtracking around the whole thing.

1. **Describe once (OKF).** Each source lives in `sources/<name>/` as an OKF document —
   Markdown with YAML frontmatter that is *actionable*: it carries the endpoint, the query
   operations (as URL templates), the response shape, the source's capability, and worked example
   queries. Adding a source is adding a folder; there is no code to write.

2. **Discover (ARD).** `registry/index.py` embeds every table's representative queries into a
   vector index. The Agent Finder (`agent_finder.py`) embeds the full question and its
   entity-expunged measure in one provider request, unions the closest tables, and performs one
   low-reasoning, output-bounded rerank. This is the ARD layer: the question only ever sees the one
   table that matches, which is why thousands of small descriptors beat one giant schema.

3. **Plan before fetch (`planner.py`).** The engine classifies the question's *shape* — point,
   status, entity-list, comparison, timeseries, ranking, aggregate, filtered-subset, ratio,
   topical, correlation — and checks it against the *capability* each candidate source declares
   (keyed point read, predicate search, population scan, server-side aggregate, …). A question the
   source structurally cannot answer is **refused before any request is issued**, rather than
   answered with something plausible but wrong. (Example: a source that lists a nonprofit's grants
   can *compare* two named orgs but cannot *rank* the whole population — so a ranking question over
   it is refused, not faked.)

4. **Fetch (generic accessor).** One fetcher fills the chosen operation's URL template and calls
   the API live. No source-specific fetch code.

5. **Check + backtrack.** The result is checked to confirm it's actually *about* the thing asked
   (right measure, unit, currency, entity). If not, the search backtracks to the next candidate
   table, entity, or period. This turns "no data" into "no *wrong* data."

6. **Synthesize.** The chat model writes a grounded answer, quoting the figure and citing the
   source named in the data — never fact-checking the value itself (its training cutoff makes
   recent data look wrong), only how it's phrased.

Supporting pieces: a demand-driven **commons** (`store.py`, `store_backends.py`) that materializes
normalized observations keyed on a shared entity spine (FIPS, EIN, QID) so a cross-source join is
paid once; and pluggable **storage backends** (sqlite locally, Postgres/BigQuery in the cloud,
selected by environment).

---

## The IRS 990 grant graph

The centerpiece relational source. Every 990 filing that makes grants lists them — public
charities in Schedule I, private foundations in 990-PF Part XV — and each grant is an edge:
`funder → recipient (amount, purpose, year)`. `grants.py` traverses that edge table in both
directions and answers exploratory questions over the whole graph:

- **Forward** — *"Who does the Gates Foundation fund?"* → its recipients, biggest first.
- **Reverse** — *"Which foundations fund Stanford?"* → its funders.
- **Rankings** — biggest grantmakers; biggest recipients; most-connected recipients (in-degree).
- **Geography** — grant dollars by recipient/funder state; flow between two states.
- **Graph patterns** — organizations that two funders *both* support (intersection).
- **By cause** — grant dollars by NTEE category (education, health, environment, …).
- **Aggregates** — totals, averages, per-year trend, threshold subsets.

The grant queries need a one-time data build (kept out of the repo — it's derived and large):

```bash
python3 tools/grants_download.py   # streams the IRS 990 e-file XML for 2022-2024 (~13 GB over
                                   # the wire, ~1-2 h) one monthly ZIP at a time, parsing out the
                                   # grant edges -> data/990/grants.sqlite (~7M edges). Resumable.
python3 tools/bmf_ntee.py          # adds the IRS BMF NTEE lookup, for the by-cause queries
```

Peak *disk* is only a few hundred MB (one ZIP at a time), but it downloads the full 2022-2024 e-file
corpus (26 monthly ZIPs, ~13 GB), so budget the bandwidth and hour or two above. It's resumable — re-run to continue where it stopped.
**While the build is running the grant questions stay unavailable** (the SQLite writer holds the
database, so the read-only query path sees "database is locked"); let it finish first.

The rest of the engine works without this; only the grant-graph questions depend on it.

---

## Data sources

Described in `sources/`. Each is one OKF document; the "tables" column is how many individual
measures/leaves it exposes.

| Source | Covers | Tables |
|---|---|---|
| `sec-edgar` | US public company financials (us-gaap concepts) | ~8,100 |
| `census` | ACS demographics/economics/housing by place | ~510 |
| `treasury` | Federal debt, rates, exchange rates | ~180 |
| `nonprofit-990` | IRS 990 nonprofit financial fields | ~57 |
| `cdc-places` | Local health measures by county/city | ~40 |
| `irs-grants` | **The 990 grant graph — who funds whom** | (relational) |
| `nonprofit-bmf` | 501(c) status, ruling, NTEE, eligibility | 6 |
| `nonprofit-profile` | Mission, leadership (via Wikidata) | 7 |
| `college-scorecard` | Tuition, admissions, outcomes by college | 6 |
| `usaspending`, `nih-reporter`, `nsf-awards`, `grants-gov` | Federal awards & grant opportunities | keyed |
| `sec-bq`, `irs-990-bq`, `census-acs-bq` | BigQuery population rankings (optional) | small |

A broader machine-readable catalog of ~100 sources and ~100 example tables is in
[`catalog/`](catalog/).

---

## Using it

**Web UI** — `http://127.0.0.1:8099/` (plus a curated `/techsoup` view and a `/sources` browser).

**HTTP API:**

```bash
curl -s localhost:8099/ask -H 'content-type: application/json' \
  -d '{"question":"Which foundations fund Emory University?"}' | python3 -m json.tool
```

Returns the answer, the shape the planner chose, the source it used, the candidate tables it
considered, and the raw data.

Interactive callers can ask the API to stop on a material ambiguity:

```bash
curl -s localhost:8099/ask -H 'content-type: application/json' \
  -d '{"query":"What was Apple’s profit in 2023?","streaming":false,"on_ambiguity":"ask"}'
```

`on_ambiguity` has three modes:

| Value | Behavior |
|---|---|
| `answer` (default) | Return the preferred answer and include fetched alternatives in structured data. |
| `ask` | Withhold the answer and return `@type: "ClarificationRequest"` with human-readable options and their fetched values. |
| `all` | Answer every materially different interpretation. |

To resolve a `ClarificationRequest`, repeat the original query and copy the chosen option's
`assumptions` object into the request. For an SEC measure this includes the exact concept, so the
follow-up does not run semantic selection a second time:

```json
{
  "query": "What was Apple’s profit in 2023?",
  "assumptions": {
    "measure": "net income",
    "concept": "us-gaap:NetIncomeLoss"
  }
}
```

Clarification is a completed turn, not an HTTP error: both streaming and non-streaming clients
receive it in the normal `nlws` message, with `status: "needs_clarification"`. The bundled web UI
uses `ask` and renders each option as a one-click follow-up.

**ARD discovery directly:**

```bash
python3 registry/index.py search "who funds Stanford"

# The HTTP API can retrieve several complementary phrasings with one compact rerank:
curl -s http://127.0.0.1:8088/search \
  -H 'content-type: application/json' \
  -d '{"query":{"text":"total revenue","texts":["total revenue","Apple total revenue"]},"pageSize":12}'
```

---

## Adding a source

Create `sources/<name>/_access.md` with frontmatter describing the endpoint and one or more
operations, plus per-table leaf files (or a `tools/gen_<name>.py` generator that writes them).
Rebuild the index (`python3 registry/index.py build`) and the source is discoverable — no other
code changes. The frontmatter is standard [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
plus our `access:` extension. See any existing `sources/*/_access.md` for the shape, `DESIGN.md` for
how the whole engine works, and `DESIGN-query-shapes.md` for how capabilities map to query shapes.

---

## Configuration reference

All optional except one provider's keys.

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | Force `azure` \| `openai` \| `gemini` (else auto-detected) |
| `OPENAI_API_KEY` / `CHAT_MODEL` / `EMBED_MODEL` | OpenAI provider |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Gemini provider |
| `AZURE_OPENAI_API_KEY` / `_ENDPOINT` / `_API_VERSION` / `CHAT_DEPLOYMENT` / `EMBED_DEPLOYMENT` | Azure provider |
| `GOOGLE_CLOUD_PROJECT` | Activates the BigQuery population sources (needs `gcloud` ADC) |
| `DATA_GOV_API_KEY` | api.data.gov key for College Scorecard (else rate-limited DEMO_KEY) |
| `ARD_STORE` / `DATABASE_URL` / `ARD_BUCKET` | Select a non-default storage backend for the commons |
| `GRANTS_DB` | Override the grant-graph sqlite path |

---

## Repository layout

```
sources/           OKF source descriptions (+ hand-authored leaves; generated leaves rebuilt on first run)
accessor/          the generic OKF-driven fetcher + skill
registry/index.py  the ARD index (build with: python3 registry/index.py build)
tools/             generators (gen_*.py) and the grant-graph ETL (grants_download.py, bmf_ntee.py)
catalog/           example ARD source/table descriptor dumps (~100 each)
llm.py             provider-agnostic chat + embeddings (Azure OpenAI | OpenAI | Gemini)
harness.py         the NL -> discover -> plan -> fetch -> check -> synthesize loop + web UI
agent_finder.py    the ARD discovery service
planner.py         query-shape x capability planning
grants.py          the IRS 990 grant-graph query engine
store.py           the materialized commons; store_backends.py selects sqlite/postgres/bigquery
run.sh             build (first run) + serve
```

---

## Troubleshooting

- **"No LLM credentials set"** — copy `set_keys.example.sh` to `set_keys.sh` and fill in one
  provider, or export the keys yourself. See [SETUP.md](SETUP.md).
- **`CERTIFICATE_VERIFY_FAILED` on first run (macOS)** — a python.org framework build ships an empty
  OpenSSL trust store. Run the installer's script once: `"/Applications/Python 3.xx/Install
  Certificates.command"`. (Homebrew/pyenv Pythons are unaffected.) See [SETUP.md](SETUP.md).
- **Queries hang / time out on a local model** — the second-stage LLM re-rank is slow on local
  models. Set `ARD_RERANK=0` (embedding-only discovery is fast and accurate). See [SETUP.md](SETUP.md).
- **Gemini stops after a few questions** — the free tier is ~20 requests/day (~3-4 questions).
  Enable billing. See [SETUP.md](SETUP.md).
- **First run is slow** — expected (~10 min): it's embedding ~8,900 table descriptors. It's
  cached; later runs are fast, and an interrupted build resumes. Rebuild with
  `python3 registry/index.py build`. To shrink it, park the SEC leaves (see [SETUP.md](SETUP.md)).
- **Grant-graph questions return nothing** — run `python3 tools/grants_download.py` (and
  `tools/bmf_ntee.py`) to build the edge table (a ~13 GB / ~1-2 h one-time download;
  grant questions stay unavailable, "database is locked", until it finishes).
- **BigQuery sources dormant / population rankings refused** — set `GOOGLE_CLOUD_PROJECT` and run
  `gcloud auth application-default login`.
- **Ports** — Agent Finder on `8088`, Web UI/API on `8099`. Stop with
  `pkill -f agent_finder.py; pkill -f 'harness.py --serve'`.
