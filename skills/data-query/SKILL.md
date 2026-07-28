---
name: data-query
description: Answer factual data questions from live authoritative US sources — public-company financials (SEC EDGAR), nonprofit finances & 501(c) status (IRS Form 990), demographics & community statistics by place (US Census ACS), local health measures (CDC PLACES), federal debt & exchange rates (US Treasury), and federal grants/awards (USAspending, NIH, NSF, Grants.gov). Use when the user asks for a specific number, rate, or status about a company, nonprofit, US place, or the federal government (e.g. "Apple's total revenue", "poverty rate in Chicago", "is the Red Cross a 501(c)(3)", "euro to dollar exchange rate", "diabetes rate in Cook County").
---

# Data query

You answer data questions by discovering the right dataset, fetching it live, checking the result,
and backtracking until it truly answers the question. **You do all the reasoning yourself** — the
tools below are deterministic and spend no model tokens (the one exception is `find`, which calls a
separate Agent Finder service that runs its own ranking model).

The engine is **plan → execute → check → backtrack**. Never hand back the first row that comes out of
a fetch; verify it actually answers what was asked, and if it doesn't, try the next candidate.

## Setup

- Tools live in one script: `python3 /Users/rvguha/code/ard-data-demo/ard.py <cmd> ...` (every command
  prints JSON; on failure it prints `{"error": ...}` and exits non-zero — read the error and backtrack).
- The Agent Finder service must be running (`http://127.0.0.1:8088`). If `find` reports it's
  unreachable, start it: `python3 /Users/rvguha/code/ard-data-demo/agent_finder.py` (needs the Azure
  keys loaded — that service is the only component that uses an LLM).

## Meta requests (answer directly — do NOT run the query loop)

If the input is about the service itself rather than a specific data question, respond directly. Treat
these like unix keywords — match the whole input, case-insensitively, with or without leading dashes:

- **`help`, `--help`, `-h`, `?`, "how does this work"** → show what this does and the command list
  (run `ard.py --help`), then point to `examples` and `sources`.
- **`sources`, `--sources`, "what data do you have", "what can you answer", "what's covered"** → run
  `ard.py sources` and list each source with what it covers and its table count.
- **`examples`, `--examples`, "give me examples", "what can I ask"** → run `ard.py examples` and show a
  few example questions grouped by source (offer to run any of them).

## The four tools

| Command | Purpose |
|---|---|
| `ard.py sources` | List the data sources and what each covers (use to scope discovery). |
| `ard.py find "<text>" [dir,dir]` | Rank OKF leaves for `<text>`, optionally scoped to source dirs. Returns `hits[].identifier`. |
| `ard.py resolve "<name>"` | Canonical ids/keys for an entity: `keys.cik`, `keys.ein`, `keys.fips_*`; for places also `place_levels[]` with ready-made census `geo` clauses (self → county → state). |
| `ard.py fetch <identifier> [k=v ...]` | Fetch the datum from one leaf; you supply the keys. |

## The loop

1. **Plan.** From the question, work out: the **entity** (company / nonprofit / place / org, or none),
   the **attribute** with the entity removed (e.g. "total revenue", "poverty rate"), and the **period**
   (a fiscal year or "latest"). Run `ard.py sources` (once per session is enough) and pick the source
   dir(s) whose coverage fits the entity type.

2. **Find.** Rank candidate leaves:
   - If the entity resolves to a **key** (a company→CIK, place→FIPS, nonprofit→EIN), search the
     **attribute** only, scoped to the source dir: `find "total revenue" sec-edgar`.
   - If the discriminator is a **dimension**, not a key (a currency, a security type — e.g. "euro"),
     search the **whole question** so that discriminator ranks its own leaf: `find "euro to dollar exchange rate" treasury`.

3. **Resolve** the entity (skip for Treasury and for questions with no named entity):
   `ard.py resolve "Apple"` → pick the right candidate by reading the descriptions, then use its keys.

4. **Execute + Check + Backtrack.** Walk the `find` hits in order. For each, `fetch` it with the
   right keys, then **check the returned record actually answers the question** (see rules below). If it
   does, stop. If not, try the next hit (or a different key/granularity). Only give up after the
   candidates are exhausted.

5. **Answer.** State the value with its unit and as-of period, and cite the record's `source`. Show any
   arithmetic. If nothing answered, say so plainly — do not invent a number.

## Fetch keys, per source

- **SEC EDGAR** (`concept` leaf): `cik=<from resolve>` (or `ticker=AAPL`). Returns `value` + `unit`
  (`USD`, `USD/shares`, `shares`, `pure`) + `period`. Add `period=FY2023` for a specific year.
- **IRS 990** (`field` / `classification` leaf): `ein=<from resolve>`.
- **Census ACS** (`variable` leaf): `geo=<clause>` — take it straight from `resolve`'s `place_levels`.
- **CDC PLACES** (`measureid` leaf): `place=<bare county or city name>` — **strip " County"** ("Cook
  County" → `place=Cook`).
- **US Treasury** (`tfield` leaf): no key. The currency/series is baked into the leaf; the record
  echoes it as `series`.
- **Grants/awards** (`search` leaf): `q="<organization or topic>"`.

## Check rules — does the record answer the question?

Judge only **what the record is about**, never the plausibility of the number. Do **not** reject a value
for looking too big/small, for an exchange rate that seems inverted, or for a date that looks "in the
future" — treat the value and its date as authoritative. Reject and backtrack only on a real mismatch:

- **Measure** differs from what was asked (e.g. "intragovernmental holdings" when the total national
  debt was asked; a poverty *subgroup* like "children 5-17" when the overall rate was asked — prefer
  the "All people" leaf).
- **Unit** is wrong for the ask: a per-share value needs a `USD/shares` unit; a "rate"/"percentage"
  needs a percent (in Census, the `...PE`/percent variable, not the count `E` estimate); an "amount"
  needs a currency value.
- **Currency** differs from the one named (euro vs China-Renminbi).
- **Place / entity** differs (a broader containing area used as a proxy is fine — see granularity).

## Backtrack patterns

- **SEC concept switch (freshness).** A company may report several revenue/income concepts; some are
  legacy and stop years ago. Fetch the top few concept candidates for the company and keep the one it
  **currently reports** — the freshest `period_end` — for a "latest" question. (E.g. Apple "total
  revenue": legacy `Revenues` stops at FY2018; `RevenueFromContractWithCustomerExcludingAssessedTax` is
  current — take the current one.)
- **Place granularity.** If a place-level fetch has no data, back off along `resolve`'s `place_levels`:
  place → county → state. (CDC PLACES often has county but not city — "diabetes in Chicago" →
  `place=Cook`, noting Cook County contains Chicago.)
- **Census percent vs count.** For a "rate"/"percentage" question pick the percent variable; the
  matching count/estimate leaf will fail the unit check.
- **Transient errors.** A network error from `fetch` is not a "no data" answer — retry the same fetch
  before moving on.

## Worked examples

**"What was Apple's total revenue?"**
`sources` → SEC. `find "total revenue" sec-edgar`. `resolve "Apple"` → `cik 0000320193`. Fetch the top
concept candidates with `cik=320193`; `Revenues` returns FY2018 (stale), `RevenueFromContractWith...`
returns FY2025 $416.16B — take the current one. Answer with the SEC citation.

**"Poverty rate in Chicago"**
`find "poverty rate" census`. `resolve "Chicago"` → `place_levels`: place `place:14000&in=state:17`.
Fetch the "All people" **percent** leaf with that `geo` → 16.9%. (The count `E` leaf would fail the
unit check; a child-poverty leaf would fail the measure check.)

**"Euro to dollar exchange rate"**
Dimension, not a key → `find "euro to dollar exchange rate" treasury` ranks the Euro leaf first. Fetch
it (no key) → `series: "Euro Zone-Euro"`, value 0.87. Confirm the series is the euro, answer.
