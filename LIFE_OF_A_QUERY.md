# The Life of a TBN Query

## Abstract

TBN answers natural-language questions over independently published data sources without prior integration. Given a question, a language model interprets it into a structured intent; the system queries an ARD index for sources capable of supplying the required data, constructs a plan over the sources it returns, queries those sources directly, performs the requisite ETL just in time, and composes an answer from the records returned. Every answer is validated against the interpreted question before it is emitted, and questions whose required operation lies outside the available access paths are refused rather than approximated. This document walks through the execution of a single query, characterizes the boundary of the answerable query class, and reports where the current implementation falls short of the guarantees the architecture admits.

Two properties are worth stating at the outset, because they are easily confused. The system is **model-directed but not model-grounded**. A language model decides what the question means, which entity it names, which measure it asks for, and what shape of operation would answer it; a model also writes the final sentence the caller reads. No model supplies a number. Every figure in an answer comes from a publisher's response that survived deterministic validation, and no model may overturn a deterministic rejection.

## 1. Introduction

Each data source is documented once using the Open Knowledge Format (OKF), encoded in JSON as an ARD entry. An entry records what kinds of data the source holds and about what entities, together with the operations its API exposes and the identifiers those operations accept. The discovery index is built over these entries. No source data is copied into the system; the index contains descriptions of sources, and the records that answer a question are retrieved from the publisher at query time.

Execution proceeds in five stages: interpretation of the question, discovery of candidate sources, construction of a plan, execution of that plan against the publishers, and validation of what returns. When a plan can be created, the system executes and verifies it before answering. Entity resolution, backtracking, and validation all occur within this loop, at query time.

Planning is necessary but not sufficient. A descriptor states which entities a source covers, what it reports about them, and which operations it supports. It does not state whether a particular record exists, and execution of a well-formed plan fails for many reasons:

  - the entity cannot be mapped to an identifier the source accepts, or the name resolves to several organizations, or to none;
  - the identifier resolves, and the source holds no record for that entity;
  - the concept or variable exists in the source's vocabulary, and this particular filer, geography, or period does not report it;
  - a value is present and suppressed, delivered as a sentinel rather than as a measurement;
  - the requested period has not yet been published;
  - pagination or rate limits truncate what a completeness claim would require;
  - the source is unavailable, or a third-party credential has been exhausted.

None of these are visible before the request is issued. The executor responds by backtracking over its remaining choices, which makes query execution a search over a decision graph rather than a fixed pipeline.

### 1.1 Where the model decides, and where it cannot

The division of labour is explicit, and it is the organizing principle of the rest of this document.

**A model decides:** what the question is about (entity, and whether the name is unambiguous); what is being asked of it (the measure or attribute); what kind of thing the entity is; which shape of operation the question requires; which published sources are plausibly relevant; whether a named measure is ambiguous and what its distinct readings are; which of several candidate records corresponds to the entity in context; whether a returned record semantically answers the question in the residual case where structure alone cannot say; and how to word the final answer from admitted evidence.

**Code decides:** which access paths each source actually exposes; whether the required operation is in the algebra at all; which identifier a given operation accepts; whether a returned record matches the resolved entity, unit, currency, period, and grain; whether a value is a measurement or a suppression sentinel; whether a population claim is complete; the order in which choice points are explored and abandoned; and every arithmetic operation.

The consequence is that a model failure degrades into a refusal or a clarification, not into a wrong number. A misclassified shape routes to an ineligible source and the plan is refused. A misidentified entity fails the entity check at validation. What a model cannot do is put a figure into an answer that no publisher returned.

## 2. Query shapes and required access paths

An **access path** is a way of getting at data that a source actually implements — a specific operation with specific parameters, such as "look up one company by CIK and one concept" or "list the counties in a state with one variable each." Access paths matter because published APIs are narrow. Census, SEC, IRS, and Treasury all expose HTTP endpoints, and those endpoints fall far short of SQL: there is generally no join, no arbitrary predicate, no grouping, and often no ordering. Whether a question can be answered therefore depends on the expressiveness of the exposed API as much as on the data behind it.

The operations a question requires are determined by its *shape*, and the shape is assigned by the classifying model. Eleven shapes are currently implemented. The list is not exhaustive — it reflects the question classes encountered so far, and new shapes are added as new access paths make them answerable.

| Shape | Example | Required access path |
| :-- | :-- | :-- |
| Point | What was Apple's total revenue in 2023? | Keyed or native read |
| Status | Is the Sierra Club a 501(c)(3)? | Relevant status field |
| Entity list | Show NSF awards received by MIT. | Keyed records-by-entity |
| Comparison | Which had more revenue in 2023, Apple or Microsoft? | Comparable keyed reads |
| Timeseries | How did Apple's revenue change from 2019 to 2024? | Period-addressable read |
| Ranking | Which states have the highest poverty rate? | Entity-grain population |
| Aggregate | How many active 501(c)(3) organizations are there? | Entity-grain population |
| Filtered subset | Which nonprofits granted more than $100 million? | Determined by quantifier |
| Ratio | What share of a nonprofit's revenue came from federal awards? | Compatible keyed measures |
| Correlation | Is county poverty associated with diabetes prevalence? | Joinable complete populations |
| Topical | Find education grant opportunities. | Predicate or search operation |

Shape assignment is consequential and is made before any source is examined. The classifier is given the distinctions that matter operationally rather than grammatically: *comparison* compares the same measure across different named entities, while *ratio* combines different measures, usually of one entity; if the entities being compared are named in the question it is a comparison, and if the engine must find them from a whole population it is a ranking or a filtered subset. These distinctions are stated to the model because they determine which sources are eligible, not because they are linguistically natural.

Grant-graph questions follow a specialized route, since the direction of the relationship being traversed determines the query.

The point query is the least demanding shape: one entity, one measure, one period, satisfiable by a single keyed read. Section 3 walks through one in full; Section 6 describes the divergences introduced by the remaining shapes.

## 3. Walking through a simple point query

The trace below is taken from the running system for the question:

```
What was Apple's total revenue in 2023?
```

### 3.1 Interpretation

A single model call reads the question and returns a structured intent. This is the most consequential call in the system: everything downstream is conditioned on it, and no later stage re-derives what it decides.

```
{
  "question":           "What was Apple's total revenue in 2023?",
  "entity":             "Apple",
  "canonical_entity":   "Apple Inc.",
  "entity_status":      "resolved",
  "entity_candidates":  [],
  "entities":           [],
  "type":               "company",
  "attribute":          "total revenue",
  "period":             "FY2023",
  "periods":            [],
  "shape":              "point",
  "quantifier":         "exhaustive",
  "threshold":          null,
  "interpretations":    [],
  "sources":            ["sec-edgar"]
}
```

Several fields deserve comment.

**`shape`** determines which access paths can serve the question, and therefore which sources are eligible before any source is examined. This question is a point read: one company, one measure, one period, which any source offering a keyed read can satisfy. Had the question been "which company had the highest revenue," the shape would be a ranking, and every keyed-read source would be ineligible regardless of how much revenue data it holds — ranking requires seeing a whole population, and reading one company at a time never produces one.

**`type`** is an open noun phrase, not a member of a closed enumeration. The model is asked for the most specific term that fits — `company`, `educational organization`, `government agency`, `nonprofit`, `person`, `place` — and is free to return one not listed. This is a deliberate reversal of an earlier design in which `type` was drawn from a fixed vocabulary of five values. The fixed vocabulary was removed because it was duplicated: the prompt offered the model one set of values while the code tested for another, narrower set, so a model returning the documented-correct value for a university could silently degrade retrieval. No code now branches on the value of `type`; it is carried as context for record selection and for display.

**`canonical_entity`** is the entity's full commonly-used name, disambiguated from the question's own context — "St. Jude" in a question about NIH research funding is "St. Jude Children's Research Hospital." **`entity_status`** records whether the model is confident which real-world entity is meant (`resolved`), whether the question genuinely does not distinguish it (`ambiguous`, populating `entity_candidates`), or whether no entity is named at all (`none`, as for an exchange rate or a national total).

**`interpretations`** is populated when the *measure* is ambiguous — when it could mean several materially different things a careful analyst would not conflate. This is the branch developed in Section 4.

**`sources`** is the model's proposal of which source families are plausibly relevant, drawn from a list of directories and the entity types each covers. It narrows discovery; it does not determine it, and it is intersected with what the index actually returns.

### 3.2 Discovery

The entity is extracted from the routing text so that retrieval is conditioned on the measure alone:

```
Apple total revenue  →  total revenue
```

Whether the attribute or the whole question is used as the primary retrieval text depends on whether a specific named entity was identified. When one was, the attribute leads and the question is secondary; when none was — a topical search, an exchange rate — the question leads. This test is made on the presence of a resolved entity, not on its type.

The TechSoup ARD index currently covers approximately 10,400 measures, retrieving over descriptions and representative questions; other ARD deployments will index more or fewer. This query returned six SEC candidates: Revenues, several variants of Revenue from Contract with Customer, regulated revenue, and revenue net of interest expense.

These constitute candidate routes. The index records the existence and capability of sources; the value itself resides only at the publisher.

### 3.3 Capability check and mechanism selection

The planner evaluates each candidate against the shape the intent requires:

```
shape:       point
verdict:     exact
mechanism:   one keyed lookup
source:      SEC EDGAR
```

This stage is entirely deterministic. It consults declared capabilities in the OKF access document and nothing else; no model participates.

Where multiple candidates are viable, three criteria are applied in strict priority: the first criterion decides, and the second is consulted only for candidates the first leaves tied.

*An exact operation is preferred to a composed one.* SEC exposes a direct company-and-concept read returning an annual value in a single call; an alternative source exposing only quarterly figures would require summation over four values, introducing four independent opportunities for period misalignment.

*Complete coverage is preferred to declared partial coverage.* For a ranking over states, a Census table covering all fifty states is preferred to one documented as covering the fifty largest cities. The latter cannot satisfy the question at any level of effort.

*Semantic discovery order breaks remaining ties.* Where two SEC concepts both expose exact keyed reads and both cover the entity, neither criterion discriminates, and the retrieval ranking is retained.

Selection asserts capability alone. Whether the source holds this value for this entity in this period is undetermined until the request is issued.

### 3.4 Binding resolution

Apple is a name, and no publisher API accepts names. Resolution produces a shared identity carrying every identifier known for the entity:

```
{
  "qid":   "Q312",
  "label": "Apple Inc.",
  "keys":  {
    "cik":    "0000320193",
    "ticker": "AAPL",
    "ein":    "94-2404110",
    "lei":    "HWUPKR0MPOU8FGXBT394"
  }
}
```

The identity registry — Wikidata, in the current deployment — is used for exactly one purpose: to supply source-specific identifiers for an entity the model has already determined. It is never consulted to decide *what* the entity is, and it never adjudicates ambiguity. That decision belongs to the interpreting model, which has the question's context; the registry has only a name.

Where several candidate records match a name, the choice among them is a model call that receives the candidates and the original question, and is asked which is meant in this context — and is permitted to answer that several are. Name containment and string similarity are not used to make this decision.

Which identifier a source needs is not guessed: each operation in the OKF access document declares the key type it accepts. SEC company-facts declares CIK, ProPublica declares EIN, Census declares FIPS geographic codes, and a source with no standard identifier declares that it takes the label. The connector reads the declared key type and selects the matching member of the bundle. When the bundle carries no identifier of the declared type, the binding fails and the executor treats that as a choice point rather than an error, backtracking to another candidate source.

Resolution is memoized for the life of one query rather than for the process: several candidate tables in a single question commonly name the same entity, and the crosswalk is performed once and reused across them. It is deliberately not shared across requests. Resolution may return several candidates, since names are not unique — three distinct organizations are plausibly "Sierra Club" — and each candidate is a choice point to which the executor may return.

A resolved identity does not guarantee a usable one. Wikidata returns an LEI and a GNIS code for Stanford University and no College Scorecard identifier; that path falls back to matching on the institution's name. Reporting a crosswalk as successful when none of the identifiers obtained is usable by the target source is actively misleading, and progress output distinguishes the two.

### 3.5 Retrieval

The publisher is contacted only at this stage. The SEC company-facts endpoint is queried with the resolved CIK, a candidate concept, and the requested fiscal period:

```
{
  "company":    "Apple Inc.",
  "concept":    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
  "metric":     "Revenue from Contract with Customer, Excluding Assessed Tax",
  "period":     "FY2023",
  "period_end": "2023-09-30",
  "value":      383285000000,
  "unit":       "USD",
  "source":     "SEC EDGAR"
}
```

### 3.6 Backtracking

The retrieval above succeeded on its first attempt. Many do not, for the reasons enumerated in Section 1: the concept exists and this filer does not report it; the value is suppressed rather than absent; the period is not yet published; the name resolves to an organization the source has no record of. A descriptor cannot predict any of them, because it records what a source can do and not what it happens to hold.

Each execution is recorded as an Attempt carrying the source and bindings used, the raw response, elapsed time, the validation result, and a disposition of accepted, rejected, or errored. Rejected attempts are retained in the trace and are never promoted to evidence.

```
try us-gaap:AssetsNet for Tesla
   └─ 404: concept present in the taxonomy, not reported by this filer
       └─ reject the attempt
           └─ advance to the next compatible concept
```

Three failure modes observed in production:

  - SEC returns **404** for us-gaap:AssetsNet on Tesla. The concept is valid within the taxonomy and is not reported by this filer.
  - Census returns **-888888888**, a suppression sentinel delivered with HTTP 200 and indistinguishable from a measurement by type. Recovery proceeds by substituting the percentage variant of the same variable.
  - ProPublica resolves an organization name to an EIN with no incident grant edges. The traversal falls back to name matching and discloses the method used.

The choice points form a fixed ordering over which the executor backtracks:

```
candidate source
  → entity resolution
    → source key
      → concept or field
        → geography
          → period
            → fetch
```

Two dispositions are distinguished. An ordinary failure *backtracks*, advancing to the next option at the current choice point. A rejection that condemns a whole subtree *prunes* it: when a source returns a well-formed record that is adjudicated as answering a different question, no other key, geography, or period against that same source will help, and the entire subtree beneath that candidate is abandoned rather than re-explored.

Execution terminates when a path yields validated evidence, when the viable paths are exhausted, when the request is cancelled, or when a deadline or attempt limit is reached.

### 3.7 Concurrency and the execution context

Execution is asynchronous throughout, on a single event loop. There are no request-handling threads and no thread pools on the serving path; a shape that fans out — a comparison over two entities, a timeseries over six periods — issues its branches concurrently under structured concurrency, so a branch failure cancels its siblings deterministically rather than leaving them running.

All per-query state is carried explicitly in a **QueryContext** rather than in thread-locals or ambient globals: the deadline, a cancellation signal, the usage and discovery ledgers, the shared HTTP and provider clients, the progress channel, and the per-query memo used for entity resolution. Two questions in flight at the same time cannot consume one another's budget or observe one another's state.

Three limits apply, and they are different in kind. A **deadline** bounds wall-clock time for the whole query. A **per-leaf attempt cap** bounds how many bindings one search may try. A **shared per-query budget** bounds total work across all concurrent branches, so that a fan-out cannot multiply the cost of a single question without limit. Provider-specific permits — SEC's request pacing, for instance — are acquired around a single outbound call and released immediately, never held across a whole query.

Deadlines and cancellation are cooperative and are observed at every await point, so a caller that disconnects stops the work rather than merely abandoning it. Note that a context constructed without an explicit deadline has none: retry-and-backoff against a rate-limited publisher is then bounded only by the attempt caps, and can run far longer than any interactive caller would tolerate. The request path always sets one.

### 3.8 Normalization

What this stage buys is the ability to pretend, at the point of asking, that a warehouse exists.

A warehouse earns that illusion in advance and at considerable cost. Every source is mapped onto a common schema by an ETL pipeline: units reconciled, entity keys aligned, periods put on a common basis, missing-data conventions normalized. The work is substantial, it must be repeated whenever a publisher changes anything, and no question at all can be asked until the pipeline for its source has been built and is running. The payoff is that a query then sees uniform, joinable data.

TBN performs the same conversions, per response, in the connector that owns each source:

```
Census     "16.9"              → 16.9 with unit "%"
Treasury   "40033256786764.37" → 40033256786764.37 with currency USD
IRS        descriptive record  → the boolean corresponding to the question
```

The caller sees typed, unit-bearing, comparable values, as it would from a warehouse. The difference is that nothing was normalized until something was asked, so a source that is never queried costs nothing, and adding a source means writing a descriptor rather than building a pipeline. What is given up is exactly what the pre-computation buys: there is no global schema to join across, so cross-source composition must be established per query, and Section 7 records where that is not yet done rigorously.

Each conversion encodes a fact about the publisher. Only the Census connector holds the knowledge that this variable is a percentage already scaled by 100. Relocating that knowledge downstream would require the answer stage to infer from the value's form that 16.9 denotes a rate and that a large integer denotes currency. Since the answer stage is now a language model (§3.10), that relocation would be a considerably worse trade than it once was: it would ask a model to guess a unit that a connector already knows.

### 3.9 Validation

A successful HTTP response does not constitute an answer. Each structural check compares the returned record against the interpreted intent. Applied to the response in §3.5:

  - **Entity.** The filing is CIK 0000320193, the identity resolved in §3.4. A response concerning Apple Hospitality REIT fails this check.
  - **Measure.** The returned concept is a revenue concept consistent with "total revenue". A cash-flow quantity fails.
  - **Unit and currency.** USD, as declared by the concept. A value denominated in thousands but labelled USD fails.
  - **Period.** FY2023, ending 2023-09-30. Apple's fiscal year terminates in September, so a calendar-2023 value is a distinct quantity and fails.
  - **Grain.** One registrant over one period, the grain a point intent requires. A segment breakdown fails.
  - **Sentinel.** The value is a measurement rather than one of the publisher's missing-data codes.
  - **Population completeness.** Not applicable to a point read; ranking and aggregate intents must establish it.
  - **Source-specific invariants.** The concept must exist in the us-gaap taxonomy and be reported by this filer.

**This ordering is the system's principal guarantee, and it is unchanged by the expansion of the model's role elsewhere.** The structural checks run first and are decisive. A deterministic mismatch rejects the attempt, and no model may override that rejection. A model is consulted only in the residual case where every structural check has passed and semantic ambiguity remains — and it is reached only when the structural verdict explicitly marks a residual semantic question. It can reject; it cannot rescue.

Records surviving validation are promoted to Evidence:

```
{
  "kind":     "point",
  "entity":   {"label": "Apple Inc."},
  "measure":  "total revenue",
  "value":    383285000000,
  "currency": "USD",
  "period":   "FY2023",
  "source":   "SEC EDGAR"
}
```

### 3.10 Answer synthesis

**This stage has been redesigned.** Answers were previously produced by deterministic renderers selected by evidence type, with model synthesis reserved for residual cases. That is inverted: there are now no deterministic prose renderers on the answer path, and every answer is composed by a language model from admitted evidence.

The synthesis call receives the caller's original question together with the validated payload and its metadata — evidence kind, source, entity, measure, unit, currency, period, and any warnings raised during validation:

```
Apple Inc.'s total revenue for FY2023 is $383,285,000,000, according to SEC EDGAR.
```

The reason for the change is that templates produce sentences that are correct and unresponsive. A template renders the fields it was written for, in the order it was written, regardless of what was asked; it cannot answer "which is bigger" with a comparison rather than two figures, and it cannot carry a validation caveat into the sentence where the reader will see it. The question is available at this stage and a model can use it.

What the change does **not** do is let a model supply a figure. Synthesis receives admitted evidence exclusively — never the rejected attempts, never the raw publisher response, never the model's own recollection of Apple's revenue. Tables and structured payloads remain factual output rendered from the evidence, not prose. The property to test, and the one worth testing on every change to this stage, is that every figure appearing in a synthesized answer is traceable to the evidence that was admitted.

The synthesis model is configured independently of the classification and reranking models. Classification and synthesis are the two calls where quality is visible in the result; reranking is the token-heavy stage and is deliberately kept on a cheaper model.

Selection of the *shape of the answer* remains driven by the type of the admitted evidence, never by the shape assigned at classification. The distinction is load-bearing: an early implementation dispatched on the classified shape, and the question "give me an overview of the grant graph" — correctly classified, incorrectly routed to the ranking path — reported a single foundation's $39B as the total over the entire graph.

### 3.11 Observability

Because interpretation is model-directed, a failed query is far more often a bad judgment than a broken code path, and the trace has to expose the judgments. Progress is emitted as structured events over the life of a query: the entity the classifier detected and the kind of thing it took it to be; the property or measure it identified; the initial plan and the source families it proposes to search; the number of candidate tables the index returned and the top few; the crosswalk search, its candidate count, and its outcome — mapped, ambiguous, skipped, or not found; and the execution plan finally chosen.

The purpose is diagnostic, and the standard for these messages is that they must not assert more than was established. A crosswalk that obtained identifiers unusable by the target source has not succeeded, and reporting it as success sends the next reader of the trace to the wrong layer.

## 4. Ambiguity resolution

A question can be well-formed, and still fail to designate one answer. Several distinct kinds of ambiguity arise, and they are resolved at different stages.

**Entity ambiguity.** The name designates more than one entity. Three organizations are plausibly "Sierra Club"; "Apple" may be the registrant or one of its subsidiaries. This is now decided at interpretation: the classifier reports `entity_status: ambiguous` and enumerates 2–5 real-world candidates in `entity_candidates`, each a full name in the same form as `canonical_entity`, most likely first. The registry is not consulted to make this determination — a name lookup cannot know which entity a question means, and the model has the question.

**Measure ambiguity.** The measure names several published quantities. *Profit* denotes net income, operating income, or gross profit, and these differ by tens of billions of dollars. The classifier populates `interpretations` with the distinct readings. This is the case developed below.

**Relation ambiguity.** The direction of a relationship is underdetermined. "Ford Foundation grants" may request the grants Ford made or the grants Ford received, and the two traverse the graph in opposite directions.

**Temporal ambiguity.** "2023" may denote a calendar year or a fiscal year, and for a registrant whose fiscal year ends in September these are different periods over different data.

**Scope ambiguity.** "Chicago" may denote the city, the county, or the metropolitan statistical area, each a distinct geography with a distinct published value.

Entity and measure ambiguity are both surfaced to the caller. They differ in when they can be settled: an ambiguous *entity* is known before anything is fetched, while an ambiguous *measure* is adjudicated against fetched values, as described below.

*What was Apple's profit in 2023?* remains a point query; the difficulty is that **profit** denotes materially distinct SEC concepts.

The branch is entered from either of two positions. The classifier may recognize an ambiguous term prior to execution, or a source-specific resolver may determine after retrieval that several sibling concepts are reported for the entity. Both converge only once the alternatives have been fetched.

Retrieval-score proximity does not trigger the branch. Candidate interpretations are executed first, and an interpretation becomes eligible only once it has returned a value. Interpretations returning identical values are collapsed as aliases, and the surviving alternatives must differ materially before the caller is interrupted. Ambiguity is thereby adjudicated on evidence rather than on retrieval score.

The recorded trace for Apple yields three materially distinct quantities:

| Interpretation | SEC concept | FY2023 value |
| :-- | :-- | :-- |
| Net income | us-gaap:NetIncomeLoss | $96,995,000,000 |
| Operating income | us-gaap:OperatingIncomeLoss | $114,301,000,000 |
| Gross profit | us-gaap:GrossProfit | $169,148,000,000 |

The disposition is determined by the caller — the person or agent issuing the request — through an `on_ambiguity` parameter:

```
point intent: Apple / profit / FY2023
  → enumerate concrete interpretations
  → discover, resolve, fetch, and validate each
  → retain interpretations returning usable values
  → collapse same-value aliases
  → test the remainder for material difference
  → on_ambiguity
       answer (default) → answer the preferred interpretation; expose alternatives in data
       all              → return each interpretation as a separate answer
       ask              → withhold the answer; return a ClarificationRequest
```

`answer` is the appropriate default for non-interactive agents: the turn completes and the alternatives remain machine-readable. `all` serves callers preserving the ambiguity downstream. The interactive client uses `ask`, which returns an ordinary terminal message. Abridged to the fields required for resolution:

```
{
  "@type": "ClarificationRequest",
  "status": "needs_clarification",
  "original_query": "What was Apple's profit in 2023?",
  "question": "'profit' has multiple materially different published meanings for Apple. Which one do you mean?",
  "options": [
    {
      "id": "us-gaap:NetIncomeLoss",
      "label": "Net Income (Loss) Attributable to Parent",
      "value": 96995000000,
      "unit": "USD",
      "period": "FY2023",
      "assumptions": {"measure": "net income", "concept": "us-gaap:NetIncomeLoss"}
    }
  ]
}
```

Inclusion of the values is essential to the design. The choice presented is between $97.0B and $114.3B under recognizable labels, never between bare taxonomy identifiers.

Resolution proceeds by reissuing the original question with the selected option's assumptions. The concept is thereby promoted from a retrieval hint to a binding:

```
original question + {measure: net income, concept: us-gaap:NetIncomeLoss}
  → clear the classifier's interpretation list
  → bypass SEC concept reselection
  → fetch the bound concept
  → validate and admit point Evidence
  → "Apple Inc.'s net income for FY2023 is $96,995,000,000…"
```

The clarification turn resumes the original query under stronger bindings; the model is not asked to reinterpret the user's selection. The assumptions carried back must include every field needed to bind the answer — an assumption silently dropped in transit reproduces the original ambiguity and returns the caller to the same clarification, indefinitely.

The terminal model is therefore:

```
QueryIntent → Attempt[] → Evidence → Answer | Clarification
                         └─────────→ Refusal when no admissible path remains
```

## 5. The queryability boundary

The APIs available to TBN expose a finite set of access paths: lookup by canonical or native key; predicate or topical search; records belonging to one entity; ordering or top-N; complete population enumeration; filtering, grouping, or aggregation; historical or period-addressable lookup; stable join keys; and directed graph edges.

These operations constitute the system's query algebra. A question is answerable if and only if the operation it requires is a member of that algebra or composes soundly from members of it.

The boundary is similar to the one exhibited by early NoSQL stores. A store optimized around particular keys and indexes answered the query classes those structures supported; a query outside them required a new index, a materialized view, a scan, or a different engine. Improved parsing of the question does not create an access path the store does not implement. Nor does a better model: the model's role is to determine what is being asked, and no amount of interpretive skill conjures an operation the publisher does not expose.

Consider a nonprofit source exposing a single operation:

```
get_nonprofit(EIN) → one nonprofit
```

This satisfies a named point question, and satisfies a comparison over named nonprofits by repeated invocation. It cannot satisfy *which nonprofit has the highest revenue*, since it admits no enumeration of the population.

Four boundary conditions follow:

1.  **Structurally impossible.** No source exposes the required operation. A keyed read does not yield an exhaustive ranking under any composition.
2.  **Empirically unavailable.** The operation exists; no record exists for this entity, measure, or period. Backtracking exhausts the viable bindings.
3.  **Incompatible composition.** The component facts exist and cannot be combined soundly — county poverty against state diabetes prevalence, with no common grain.
4.  **Semantically underdetermined.** The operation and records exist, and the measure named admits several materially distinct referents. This is the branch of Section 4.

The first condition is detectable prior to any request:

```
{
  "shape": "ranking",
  "verdict": "infeasible",
  "reason": "no operation exposes an entity-grain population scan"
}
```

Refusal is a successful plan. The available alternatives are to rank search results as though they constituted a population, to guess identifiers until a request succeeds, or to have a model supply the missing facts from parametric memory.

A fifth condition is not a property of the architecture but is the most common cause of failure in practice, and is worth naming so it is not mistaken for one of the four: **operationally blocked**. The operation exists, the record exists, and a credential is missing or a third-party rate limit is exhausted. This is invisible at planning time and presents downstream as an ordinary retrieval failure. Because backtracking reports the exhaustion of choice points rather than the reason the last one failed, a refusal message may point at entity resolution when the true cause is an unset API key several layers below. Distinguishing these in the failure message is a current shortcoming (§7).

### 5.1 Moving the boundary by materializing an index

A structurally impossible question is impossible against the access paths that exist, and access paths can be added. Publishers commonly release data in a form that does not support the questions most often asked of it: the records are public and complete, and the only exposed operations run along the wrong axis.

Philanthropic grants are the clearest instance. The IRS publishes recent Form 990 filings as bulk XML, with grants reported through schedules such as Schedule I, organized by the filing organization. It does not expose those records as a queryable grant graph. ProPublica provides organization search and per-EIN filing data over the same corpus, and offers neither reverse traversal nor graph-wide aggregation. Yet one of the most common questions asked about a charity — *who funds it?* — is inherently a graph query, and it runs against the grain of how the data is published.

Which questions this actually blocks depends on the direction of traversal:

| Question | Global materialization required? |
| :-- | :-- |
| What grants did Ford make? | No — fetch and parse Ford's own filings |
| Who funds Stanford? | Yes — every funder's filings must be searched |
| Whom do Ford and Gates both fund? | Yes |
| Largest funders, or totals by cause | Yes |

Forward traversal is answerable directly, because the filing is indexed by the organization that made the grants. Everything else requires an inverted index over the whole corpus, and somebody has to build it.

TBN builds it: 7.8 million funder-to-recipient edges extracted from the bulk filings once and loaded into a store that exposes reverse and population operations. The specific implementation is incidental and could be replaced. What cannot be avoided, while retaining reverse and population-scale grant questions, is *some* materialized index — the operation does not exist upstream, and no amount of query planning conjures it.

This is the general remedy of Section 8 applied to a concrete case, and the cost is the one warehouses pay: the index is a copy, it goes stale, and it must be rebuilt as filings are released. It is paid for exactly one source, and only because the questions asked of that source fall outside what its publisher exposes.

## 6. Execution by shape

The control loop is invariant across shapes. The shape determines the access path required and the mechanism interposed between planning and evidence.

**Point** — *What is Chicago's poverty rate?*

```
discover an ACS poverty variable → establish Census place/variable support
  → resolve Chicago to Census geography → fetch the ACS row
  → normalize "16.9" to 16.9 with unit "%" → reject sentinels and mismatched geography
  → admit point Evidence
```

**Status** — *Is the Sierra Club a 501(c)(3)?*

```
resolve to EIN → fetch the IRS classification record
  → read the is_501c3 field → validate entity and field → admit false as valid Evidence
```

Polarity is part of the datum. A negative result must survive validation on its own terms, and a descriptive label such as "active tax-exempt organization" must not be substituted for the specific boolean under test.

**Entity list** — *Show NSF awards received by MIT.*

```
resolve the organization → fetch associated records → paginate to the declared boundary
  → disclose canonical-key versus name matching → validate identity scope and completeness
```

A source returning only its largest matches cannot support a claim of exhaustiveness.

**Comparison** — *Which had more revenue in 2023, Apple or Microsoft?*

```
resolve both entities → fan the point read out to each, concurrently
  → backtrack each child independently
  → compare returned values → report the ordering and difference
```

Comparison composes from keyed reads because the entity set is finite and supplied by the user. This does not render open-population ranking feasible. Each row carries its own period, unit, and currency through to synthesis, so a comparison is not silently rendered as a movement over time.

**Timeseries** — *How did Apple's revenue change from 2019 to 2024?*

```
resolve entity and measure once → fan out across the requested periods
  → fetch each observation under the resolved state → disclose missing periods
  → retain observations in order → compute and render the change
```

**Ranking** — *Which states have the highest poverty rate?*

```
require entity-grain population visibility → discard keyed point sources
  → prefer a source ordering by the measure, or enumerate completely and rank locally
  → validate grain and population completeness → admit ranking Evidence
```

This shape exhibits the boundary most directly. Search results do not constitute a population, and a source capable of reading one state acquires no capacity to order fifty.

**Aggregate** — *How many active 501(c)(3) organizations are there?*

```
require an entity-grain population operation
  → use a source-native aggregate where implemented (the BigQuery path counts server-side)
     or enumerate the population and count locally
  → validate scope and coverage → admit one aggregate
```

Counting the first page of search results yields a well-formed and unsound answer.

**Filtered subset** — *Which nonprofits granted more than $100 million?*

The quantifier determines the mechanism:

```
exhaustive ("which nonprofits…")   → scan the population, test every member, return all matches
existential ("give me some…")      → generate candidates, fetch a complete value for each,
                                     test the threshold, terminate once sufficient
```

Generate-and-test satisfies the existential form and cannot establish exhaustiveness.

**Ratio** — *What share of a nonprofit's revenue came from federal awards?*

```
decompose into component measures → discover and fetch each independently
  → compute in code → compare periods, completeness, and name-match scope
  → derive the ratio → return components, formula, and alignment warnings
```

Two individually valid facts do not entail a valid ratio; their semantics must additionally be compatible. The decomposition into components is a model judgment, and it is the failure point of this shape in practice: a question the model declines to decompose is refused before any source is consulted.

**Correlation** — *Is county poverty associated with diabetes prevalence?*

```
require two complete entity-grain population sources declaring county grain
  → materialize both series over the scope → align rows on canonical geography keys
  → handle missing observations → compute correlation and sample size
```

This branch refuses sources without county grain, refuses suppressed series, requires at least three aligned rows, and reports ecological and spatial-autocorrelation caveats. It also declines to materialize a series whose declared row count would be disproportionate to a single question, on the grounds that such a series should be materialized once per vintage rather than per query.

**Topical** — *Find education grant opportunities.*

```
select a search operation → fetch relevant records → paginate within declared limits
  → return ranked matches
```

Relevant examples constitute a complete answer under this shape, and remain distinct from a population statistic.

**Grant graph** — *Which foundations fund Stanford?*

Relationship direction determines the query, so the graph carries its own mechanism selector:

```
who does X fund?           → forward traversal
who funds X?               → reverse traversal
whom do X and Y both fund? → intersection
largest funders/recipients → graph-wide ranking
money from A to B          → geographic aggregation
funding by cause           → edge-to-NTEE join
```

For Stanford:

```
discover the "who funds an organization" descriptor → select reverse traversal
  → resolve Stanford → prefer an exact recipient EIN present in the edge table
  → fall back to recipient-name matching only where necessary → fetch incoming edges
  → aggregate by funder → disclose the match method → render funders, totals, and provenance
```

## 7. Current implementation boundaries

The point and status paths implement the connector, validation, evidence, and answer boundaries in full. The composed paths do not yet meet the same standard:

  - **Comparison** relies on a common attribute across child queries and does not establish that their results share units and periods. *Revenue in thousands from one source compared against revenue in units from another is reported as a comparison, not refused.*
  - **Timeseries** fetches through the resolved strategy and does not admit independent evidence per observation. *A 2019–2024 revenue series is admitted as one object, so a single restated year cannot be rejected without discarding the series.*
  - **Aggregate** planning admits a broader class than the source-native aggregate executors implement. *A count the planner accepts as feasible can reach an executor with no server-side aggregate, which then enumerates a population it was not sized for.*
  - **Ratio** emits warnings where incompatible units, currency, grain, or entity keys should produce refusal. *A numerator matched by organization name against a denominator matched by EIN returns a percentage with a warning attached, rather than a refusal.*
  - **Correlation** enforces county grain and shared keys, and does not enforce common period basis or unit semantics. *A 2019 poverty series correlated against a 2022 prevalence series is computed and reported; absent a resolved state, the scope silently defaults to California.* The correlation path also assumes an HTTP accessor, and a measure best served by a warehouse-backed descriptor is routed into it incorrectly.

Two further shortcomings follow from the expanded role of the model and are properties of the current implementation rather than of the architecture:

  - **Refusal messages report the exhaustion of choice points rather than the cause of the last failure.** A query blocked by an unset third-party credential reports "no viable hit (no viable entity …)", which points a reader at entity resolution. The underlying status — a 429, a missing key — is known at the point of failure and is not carried into the message.
  - **A model judgment and a system defect are not distinguishable from the outside.** A question the classifier declines to decompose, a measure it misreads as a percentage, and a genuinely unsupported access path all present as a refusal. The progress trace (§3.11) exists to make the distinction visible, and it is the only thing that does.

These are addressable within the existing validation ladder and message paths.

## 8. Extending the answerable class

A structurally impossible query requires a new capability; prompt engineering does not substitute for one. Depending on the missing operation, this means adding a population endpoint or BigQuery table, adding an index, rollup, or materialized view (§5.1), exposing stable entity keys, adding historical addressing, loading directed graph edges, or implementing a composition under explicit compatibility checks.

The new operation must then be declared in the source's OKF access document, since the planner selects only over declared capabilities.

The converse also holds, and is the more common case now that interpretation is model-directed: a question that *is* within the algebra may still fail because the interpreting model misread it. That is not a capability gap and is not repaired by adding a source. It is repaired in the prompt, or by using a more capable model for the call in question — the classification call, in particular, is the one on which every subsequent stage depends, and it is the one where a small, cheap model is a false economy.

## 9. Summary

TBN is a bounded query planner over the access paths that published APIs expose. A language model interprets the question into a structured intent — entity, measure, shape, period, and the ambiguities it can detect; semantic discovery proposes candidates; declared capabilities constrain them deterministically; resolution parameterizes the selected path, using an identity registry solely to obtain source-specific identifiers; retrieval obtains facts; execution searches the remaining choice points concurrently under an explicit per-query context; deterministic validation admits or rejects, and no model may overturn a rejection; ambiguity is resolved against fetched values; a model composes the final answer from admitted evidence alone; and refusal reports the boundary that was reached.

The model decides what is being asked and how to say the answer. It does not decide what is true.
