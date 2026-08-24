# The life of a query

Resource Raiser does not turn every understandable question into an answer. It turns a question
into a required data operation, asks whether the available source APIs can express that operation,
and then either executes it with evidence or explains why it cannot be done.

Search mostly has one job: retrieve and rank relevant things. Resource Raiser has branching logic.
A point lookup, a population ranking, a ratio, a correlation, and a grant-graph traversal require
different access paths. Even after a viable path is found, live data can force another branch: a
company may not report a concept, Census may return a suppression sentinel, or a nonprofit
resolution may have no grant edges.

The life of a query is therefore a decision graph, not a pipeline.

This document has four layers. First, it follows one point query all the way through the running
system. Second, it follows a materially ambiguous query through fetched alternatives and an
optional clarification turn. Third, it defines the queryability boundary—the point at which the
available APIs make a question impossible. Fourth, it gives a compact “life of…” for every
implemented generic shape and for the specialized grant-graph route. Where composed paths have
weaker validation than the point path, the text says so explicitly.

```text
question
   │
   ▼
interpret intent ──► identify query shape ──► derive required capability
                                                   │
                                      ┌────────────┴────────────┐
                                      │                         │
                              capability exists         capability absent
                                      │                         │
                           discover and order routes       explain refusal
                                      │
                              resolve source bindings
                                      │
                                  fetch live data
                                      │
                    ┌─────────────────┼─────────────────┐
                    │                 │                 │
                no record       invalid result      usable result
                    │                 │                 │
                    └────── backtrack to a choice       ▼
                                                   normalize
                                                       │
                                                    validate
                                              ┌────────┴────────┐
                                              │                 │
                                            reject           Evidence
                                              │                 │
                                          backtrack     material ambiguity?
                                                        ┌───────┴────────┐
                                                        │                │
                                                       no               yes
                                                        │                │
                                                      render       ambiguity policy
                                                        │        ┌───────┼───────┐
                                                        ▼        │       │       │
                                                      Answer   answer    all     ask
                                                                 │       │       │
                                                                 ▼       ▼       ▼
                                                               Answer  Answers  Clarification
                                                                                  │
                                                                          chosen assumption
                                                                                  │
                                                                            resume query
```

## One query, end to end

Consider a point question that the running system has answered:

> What was Apple's total revenue in 2023?

### 1. Interpret the request

The classifier produces a small `QueryIntent` rather than a multi-stage plan hierarchy:

```json
{
  "question": "What was Apple's total revenue in 2023?",
  "operation": "point",
  "entity": "Apple",
  "entity_type": "company",
  "measure": "total revenue",
  "period": "FY2023",
  "quantifier": "exhaustive"
}
```

The decisive field is `operation`. A point question can use a keyed lookup. A ranking question
cannot, because it must see a population. Classification chooses which part of the query graph is
even eligible.

### 2. Discover possible data resources

The entity is removed from the routing text so discovery can concentrate on the measure:

```text
Apple total revenue  →  total revenue
```

The ARD Agent Finder searches descriptions and representative questions for roughly 8,900
measures. In the observed Apple run it returned six SEC candidate tables. They included `Revenues`,
several forms of `Revenue from Contract with Customer`, regulated revenue, and revenue net of
interest expense.

These are possible routes, not possible answers. The ARD index stores descriptions of data. It
does not store Apple's revenue.

### 3. Check capability and select a mechanism

The planner asks what each candidate's API can actually do. For this question it selects:

```text
shape:       point
verdict:     exact
mechanism:   one keyed lookup
source:      SEC EDGAR
```

Across viable candidates, ordering is lexicographic:

1. prefer an exact operation over a composed one;
2. prefer complete population coverage over declared partial coverage;
3. preserve semantic discovery order as the final tie-break.

### 4. Resolve source-specific bindings

`Apple` is not an SEC key. Entity resolution produces a shared identity with source-specific keys:

```json
{
  "qid": "Q312",
  "label": "Apple Inc.",
  "keys": {
    "cik": "0000320193",
    "ticker": "AAPL",
    "ein": "94-2404110",
    "lei": "HWUPKR0MPOU8FGXBT394"
  }
}
```

The SEC branch uses CIK. A nonprofit branch would use EIN; Census uses FIPS; another source might
use the QID or a native name. Resolution can yield several candidates, each of which becomes a
choice the executor can revisit.

### 5. Fetch the fact

Only now does Resource Raiser ask the publisher for data. It queries SEC company facts using the
resolved CIK, candidate concept, and requested fiscal period.

The successful live response was:

```json
{
  "company": "Apple Inc.",
  "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
  "metric": "Revenue from Contract with Customer, Excluding Assessed Tax",
  "period": "FY2023",
  "period_end": "2023-09-30",
  "value": 383285000000,
  "unit": "USD",
  "source": "SEC EDGAR"
}
```

### 6. Record the attempt and follow the data

Every execution becomes an `Attempt`. It records the source and bindings, the raw response, how
long execution took, its validation result, and whether it was accepted, rejected, or errored.

The happy path is only one branch. A real SEC lookup can do this instead:

```text
try us-gaap:AssetsNet for Tesla
   └─ no reportable data
       └─ reject that attempt
           └─ try the next compatible concept
```

The descriptor can establish that SEC supports company/concept lookup. It cannot predict which
concept a particular company reports. That is why empirical retry remains underneath planning.

The same pattern appears elsewhere:

- Census returns `-888888888`, a missing/suppressed sentinel rather than a value;
- a requested year has not been published, so the period branch is exhausted;
- a nonprofit name resolves to an EIN with no grant edges, so traversal tries its documented
  name-match fallback and discloses the match method.

A rejected `Attempt` remains in the trace. It never becomes evidence.

### 7. Normalize at the connector boundary

Source APIs disagree about representation. Connectors convert source-specific responses into
typed candidate evidence before validation and rendering:

```text
Census       "16.9"              → 16.9 with unit "%"
Treasury     "40033256786764.37" → 40033256786764.37 with currency USD
Postgres     Decimal(...)        → a JSON-safe integer or float
IRS status   descriptive record  → the boolean relevant to the question
```

The renderer does not infer that `16.9` looks like a percentage or that a long number looks like
dollars. Those are source semantics and belong in the connector.

### 8. Validate before admitting evidence

A successful HTTP or SQL response is not automatically an answer. Structural validation compares
the candidate with the intent:

```text
suppression sentinel   pass
unit                   pass
currency               pass
period                 pass
entity                 pass
measure                pass
SEC concept            pass
```

Checks cover entity identity, measure, unit, currency, period, grain, missing-value sentinels,
population completeness, and source-specific invariants. A deterministic mismatch rejects the
attempt. The model cannot override it. An LLM acceptance check is consulted only when structural
evidence leaves genuine semantic ambiguity.

An accepted response becomes `Evidence`:

```json
{
  "kind": "point",
  "entity": {"label": "Apple Inc."},
  "measure": "total revenue",
  "value": 383285000000,
  "currency": "USD",
  "period": "FY2023",
  "source": "SEC EDGAR"
}
```

### 9. Render from evidence

Validated evidence type, not the classifier's original shape, selects the deterministic renderer.
The point renderer preserves the resolved entity, the user's measure, period, unit, formatted
value, and source:

> Apple Inc.'s total revenue for FY2023 is $383,285,000,000, according to SEC EDGAR.

Point, status, ranking, threshold, timeseries, and the principal grant directions have
deterministic renderers. Complex evidence can fall back to grounded LLM synthesis, but synthesis
receives admitted evidence rather than an unchecked API response.

### 10. Deliver the answer and its history

While the graph is being explored, the server emits live NLWeb/SSE messages: interpretation,
discovery, mechanism, resolution, fetch, validation, backtracking, and completion. The completed
response includes:

```text
QueryIntent → Attempt[] → Evidence → Answer
```

It also carries source candidates, provenance, renderer, latency, token use, and cost. The UI can
show the chosen interpretation, rejected attempts and their reasons, individual validation checks,
and editable assumptions.

## The life of an ambiguous query

Ambiguity is not a twelfth query shape. It is a branch that can overlay a point, status, or
entity-list query when the requested measure does not uniquely identify one published fact.
`What was Apple's profit in 2023?` is still a point query; the problem is that *profit* can refer to
materially different SEC concepts.

The branch has two entry points. The classifier can recognize an ambiguous phrase such as *profit*
before execution, or a source-specific resolver can discover after retrieval that several sibling
concepts are actually reported for the entity. Both paths converge only after fetching alternatives.

The system does not ask merely because two descriptors have similar scores. It first executes the
candidate interpretations. An option is eligible for clarification only when it returned a real
value. Same-value aliases are collapsed, and alternatives must differ materially before a human is
interrupted. This makes ambiguity an evidence question rather than a retrieval-score question.

The live Apple trace produced:

| Interpretation | SEC concept | FY2023 value |
|---|---|---:|
| Net income | `us-gaap:NetIncomeLoss` | $96,995,000,000 |
| Operating income | `us-gaap:OperatingIncomeLoss` | $114,301,000,000 |
| Gross profit | `us-gaap:GrossProfit` | $169,148,000,000 |

Those values are not synonyms. Once the alternatives have been fetched, the request's
`on_ambiguity` policy selects one of three terminal branches:

```text
point intent: Apple / profit / FY2023
  → enumerate concrete interpretations
  → discover, resolve, fetch, and check each interpretation
  → retain only alternatives with usable returned values
  → collapse same-value aliases
  → compare the remaining values for material difference
  → on_ambiguity
       answer (default) → render the preferred interpretation; expose all alternatives in data
       all              → return every interpretation as a separate answer
       ask              → withhold the answer; return ClarificationRequest
```

`answer` is the safe default for non-interactive agents: it completes the turn and leaves the
alternatives machine-readable. `all` is useful when the caller wants the ambiguity preserved in
the answer. The bundled interactive UI uses `ask`.

The `ask` response is a normal NLWeb terminal message, not an HTTP error. The block below is
abridged: it includes the fields a client needs to recognize and resolve the clarification, while
omitting the ordinary items, trace, evidence, usage, and planning fields that accompany it.

```json
{
  "@type": "ClarificationRequest",
  "status": "needs_clarification",
  "original_query": "What was Apple's profit in 2023?",
  "question": "“profit” has multiple materially different published meanings for Apple. Which one do you mean?",
  "options": [
    {
      "id": "us-gaap:NetIncomeLoss",
      "label": "Net Income (Loss) Attributable to Parent",
      "value": 96995000000,
      "unit": "USD",
      "period": "FY2023",
      "concept": "us-gaap:NetIncomeLoss",
      "assumptions": {
        "measure": "net income",
        "concept": "us-gaap:NetIncomeLoss"
      }
    },
    {
      "id": "us-gaap:OperatingIncomeLoss",
      "label": "Operating Income (Loss)",
      "value": 114301000000,
      "unit": "USD",
      "period": "FY2023",
      "concept": "us-gaap:OperatingIncomeLoss",
      "assumptions": {
        "measure": "operating income",
        "concept": "us-gaap:OperatingIncomeLoss"
      }
    }
  ]
}
```

The values in the options are essential. A person should choose between `$97.0B net income` and
`$114.3B operating income`, not between unexplained taxonomy identifiers.

When the user chooses net income, the client repeats the original question with the option's
`assumptions`. The exact concept is now a binding, not another hint to semantic search:

```text
original question + {measure: net income, concept: us-gaap:NetIncomeLoss}
  → clear the classifier's old interpretation list
  → bypass SEC concept reselection
  → fetch the chosen concept
  → validate and admit point Evidence
  → "Apple Inc.'s net income for FY2023 is $96,995,000,000…"
```

The clarification turn therefore resumes the same query with stronger bindings. It does not start
an unrelated conversation and it does not ask the model to interpret the user's choice again.
The common terminal model is now:

```text
QueryIntent → Attempt[] → Evidence → Answer | Clarification
                         └─────────→ Refusal when no admissible path remains
```

Answers and clarifications carry attempts and evidence. A refusal carries the reason the plan or
execution boundary was exhausted; in the current protocol it is emitted as an explicit error
message rather than the same evidence payload. Clarification is the only terminal outcome designed
to resolve into a more specific query and then an `Answer`.

## The queryability boundary

Understanding a question and being able to execute it are different things. The APIs available to
Resource Raiser expose a finite set of access paths:

- lookup by a canonical or native key;
- predicate or topical search;
- records belonging to one entity;
- ordering or top-N;
- complete population enumeration;
- filtering, grouping, or aggregation;
- historical or period-addressable lookup;
- stable join keys;
- directed graph edges.

Together these operations form the system's query algebra. A query is answerable only when its
required operation exists in that algebra or can be safely composed from operations that do.

This resembles the boundary in early NoSQL systems. A store optimized around particular keys and
indexes could answer the corresponding query classes. A query outside those access paths required
a new index, a materialized view, a scan, or another engine. Better natural-language parsing did
not create a missing access path.

### The life of an impossible query

Suppose the only nonprofit API operation is:

```text
get_nonprofit(EIN) → one nonprofit
```

That source can answer a named point question. It can also compare a finite set of named nonprofits
by making several point calls. It cannot determine which nonprofit has the most revenue, because it
cannot enumerate the nonprofit population.

Resource Raiser distinguishes four boundaries:

1. **Structurally impossible.** No source exposes the required operation. A keyed lookup cannot
   produce an exhaustive population ranking.
2. **Empirically unavailable.** The operation exists, but this entity, measure, or period has no
   record. Backtracking exhausts the viable bindings.
3. **Incompatible composition.** Component facts exist but cannot be combined honestly—for
   example, county-level poverty and state-level diabetes data with no valid common grain.
4. **Semantically underdetermined.** The operation and records exist, but the measure maps to
   materially different facts. The caller must accept, inspect, or resolve the alternatives.

Structural impossibility should be detected before fetching:

```json
{
  "shape": "ranking",
  "verdict": "infeasible",
  "reason": "no operation exposes an entity-grain population scan"
}
```

Refusal is a successful plan. It is preferable to ranking search results as though they were a
population, repeatedly guessing identifiers, or asking a model to manufacture the missing facts.

## From the common life to the life of each shape

The common control loop stays the same; the query shape changes its required access path and the
mechanism between planning and evidence.

## The life of a point query

Example: **What is Chicago's poverty rate?**

```text
point intent
  → discover an ACS poverty variable
  → establish that Census supports place/variable lookup
  → resolve Chicago to Census geography
  → fetch the ACS row
  → normalize "16.9" to 16.9 with unit "%"
  → reject suppression sentinels or wrong geography
  → admit point Evidence
  → render "Chicago's poverty rate is 16.9%…"
```

The branch fails structurally if no source supports a keyed/native read, and empirically if every
viable source lacks a value for the entity and period.

## The life of a status query

Example: **Is the Sierra Club a 501(c)(3)?**

```text
status intent
  → resolve Sierra Club to EIN
  → fetch the IRS classification record
  → select is_501c3, not a friendly general-status label
  → validate entity and field
  → admit false as valid Evidence
  → render "No…"
```

The polarity is data. A negative answer must not be rejected merely because it is negative, and
descriptive text such as “active tax-exempt organization” must not replace the question-specific
boolean.

## The life of an entity-list query

Example: **Show NSF awards received by MIT.**

```text
entity-list intent
  → resolve the organization
  → fetch its associated records
  → follow pagination to the declared boundary
  → disclose canonical-key versus name matching
  → validate identity scope and completeness
  → admit a record collection
```

A source that returns only its largest matches cannot support a claim that the list is exhaustive.

## The life of a comparison query

Example: **Which had more revenue in 2023, Apple or Microsoft?**

```text
comparison intent
  → resolve both companies
  → fan out the same point lookup to each entity
  → independently backtrack each child lookup
  → compare returned numeric values
  → report the winner and difference
```

## The life of a timeseries query

Example: **How did Apple's revenue change from 2019 to 2024?**

```text
timeseries intent
  → resolve entity and measure once
  → fan out across requested periods
  → fetch each observation using the resolved state
  → reject or disclose missing periods
  → retain numeric observations in requested order
  → compute and render change
```

## The life of a ranking query

Example: **Which states have the highest poverty rate?**

```text
ranking intent
  → require entity-grain population visibility
  → discard keyed point sources
  → prefer a source that orders by the measure
     or enumerate completely and rank locally
  → validate grain and population completeness
  → admit ranking Evidence
```

## The life of an aggregate query

Example: **How many active 501(c)(3) organizations are there?**

```text
aggregate intent
  → require an entity-grain population operation
  → use a source-native aggregate where implemented (the BigQuery path counts server-side)
     or enumerate a population for client-side work
  → validate scope and coverage
  → admit one aggregate
```

## The life of a filtered-subset query

Example: **Which nonprofits granted more than $100 million?**

The quantifier changes the mechanism:

```text
exhaustive (“which nonprofits…?”)
  → complete population scan
  → test the threshold for every member
  → return all matches within the declared scope

existential (“give me some nonprofits…”) 
  → generate candidates
  → fetch a complete value for each candidate
  → test the threshold
  → stop after enough verified examples
```

Existential questions are answerable from weaker capabilities. Generate-and-test cannot prove that
it found every match.

## The life of a ratio query

Example: **What share of a nonprofit's revenue came from federal awards?**

```text
ratio intent
  → split the request into component measures
  → discover and fetch each component independently
  → compute in code rather than in the model
  → compare periods, completeness, and name-match scope
  → derive the ratio
  → return component provenance, formula, and alignment warnings
```

Two individually valid facts do not imply a valid ratio; their semantics must also be compatible.

## The life of a correlation query

Example: **Is county poverty associated with diabetes prevalence?**

```text
correlation intent
  → require two complete entity-grain population sources
  → select two sources that declare county grain
  → materialize both series for the selected state scope
  → align rows on canonical geography keys
  → handle missing observations
  → compute correlation and sample size
```

This branch refuses sources without county grain, refuses unusable or suppressed series, requires
at least three aligned rows, and reports ecological and spatial-autocorrelation caveats.

## The life of a topical query

Example: **Find education grant opportunities.**

```text
topical intent
  → select a search operation
  → fetch relevant records
  → paginate within the source's declared limits
  → return ranked matches
```

This is the branch most like conventional search. It can return relevant examples without claiming
to have computed a population statistic. “Find some” and “find every” remain different queries.

## The life of a grant-graph query

Example: **Which foundations fund Stanford?**

The grant graph has its own mechanism selector because relationship direction changes the query:

```text
who does X fund?          → forward traversal
who funds X?              → reverse traversal
whom do X and Y both fund?→ intersection
largest funders/recipients→ graph-wide ranking
money from A to B         → geographic aggregation
funding by cause          → edge-to-NTEE join
```

For Stanford, the route is:

```text
discover the “Who funds an organization” descriptor
  → select reverse traversal
  → resolve Stanford
  → prefer an exact recipient EIN represented in the edge table
  → fall back to recipient-name matching only when necessary
  → fetch incoming grant edges
  → aggregate by funder
  → disclose whether the match used EIN or name
  → render funders, totals, and provenance
```

The edge store is a necessary materialization because the IRS publishes bulk filings rather than a
live relationship-query API. If neither the local SQLite graph nor configured Postgres graph is
available, the traversal is operationally unavailable even though the intent is understood.

## Current implementation boundaries

The point and status paths have the strongest connector, validation, evidence, and rendering
boundaries. Composed paths are not yet equally strict:

- **Comparison** uses a common attribute in its child queries but does not yet prove unit and period
  compatibility across their results.
- **Timeseries** fetches periods through the resolved strategy but does not admit independent
  `Evidence` for every observation.
- **Aggregate** planning is broader than the mature source-native aggregate executors.
- **Ratio** warns about period, completeness, and name alignment, but does not yet enforce unit,
  currency, grain, and entity-key compatibility across components.
- **Correlation** enforces county grain and common keys, but not common period basis or units; with
  no resolved state, its current scope defaults to California.

## Backtracking is shared; mechanisms are not

Each shape enters a different mechanism, but empirical choice points recur:

```text
candidate source
  → entity resolution
    → source key
      → concept or field
        → geography
          → period
            → fetch
```

The executor explores only choices relevant to the selected source. It stops when one path yields
validated evidence, when all viable paths are exhausted, when the request is cancelled, or when its
deadline or attempt limit is reached.

Planning and execution therefore answer different questions:

- **Planning:** can this API express the required operation?
- **Execution:** does this path yield usable data for this entity, measure, and period?

Neither can replace the other.

## Adding an answerable query class

When a query is structurally impossible, the remedy is a new capability, not a more persuasive
prompt. Depending on the missing operation, that may mean:

- adding a population endpoint or BigQuery table;
- adding an index, rollup, or materialized view;
- exposing stable entity keys;
- adding historical addressing;
- loading directed graph edges;
- or implementing a safe composition with explicit compatibility checks.

The new operation must be described in the source's OKF access document so the planner can see it.

## The compact thesis

Resource Raiser is not a search box placed in front of APIs. It is a bounded query planner over the
access paths those APIs actually expose.

> Semantic discovery proposes. Capabilities constrain. Resolution parameterizes. Fetching obtains
> facts. Execution explores. Validation admits. Ambiguity asks or exposes alternatives. Evidence
> renders. Refusal explains the boundary.
