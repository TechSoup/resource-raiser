# Critique — `LIFE_OF_A_QUERY.md`

Reviewed against the running system at `https://tsnlw.centralindia.cloudapp.azure.com/rr`
(commit `0a2eccf`). Claims below were executed, not read.

## What works

The document takes the branching point seriously, and that was the hard part. "The life of a query
is therefore a decision graph, not a pipeline" is the right thesis, the ASCII decision graph earns
its place, and the three-layer structure — one trace, then the boundary, then a life per shape — is
the correct shape for a system whose *plan is the output*.

**The queryability boundary is the best section in the document.** The framing that the available
APIs form a query algebra, and that a question is answerable only if its required operation is in
that algebra, is the idea most worth putting in front of a launch audience. The NoSQL analogy lands:
better parsing does not create a missing access path. The three-way split — structurally
impossible / empirically unavailable / incompatible composition — is a genuinely useful distinction
that most systems in this space never draw.

The honesty is also right. Saying outright that ratio and comparison paths validate less strictly
than the correlation path, and calling it "a present limitation, not a solved part of the boundary,"
is worth more to a technical reader than a uniformly confident document.

Three claims I checked and found accurate:

- **Refusal** — `"Which nonprofit has the highest revenue?"` returns
  `this is a 'ranking' question, which needs a source that can see a whole population;
  nonprofit-990 no operation exposes an entity-grain population scan`, before any fetch. Matches §
  *The life of an impossible query*.
- **Entity resolution** — the shared-identity block with `cik` / `ticker` / `ein` / `lei` is real;
  the running system emits exactly those keys for Apple.
- **The shapes table** matches `planner.SHAPES` and the implemented verdicts.

## The blocking problem: the headline example is not reproducible

§ *One query, end to end* opens with

> Consider a point question that the running system has answered:
> **What was Apple's revenue in 2023?**

and later prints evidence of `383285000000`. Run that exact question against the running system:

```
"What was Apple's revenue in 2023?"  →  $8,200,000,000
                                        us-gaap:ContractWithCustomerLiabilityRevenueRecognized
"Apple total revenue in 2023"        →  $383,285,000,000
                                        us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax
```

Apple's FY2023 net sales were $383.285B. The value in the document is correct; **the question
printed above it produces a different answer — wrong by a factor of 47.** Dropping the word "total"
selects a sibling concept (deferred revenue recognized in the period, not revenue).

This matters beyond a citation fix, for three reasons:

1. **A document that says "the running system has answered" must be reproducible.** This is the same
   lesson as the 0.62% descriptor figure: a number quoted without a recorded command and commit
   drifts away from the system it describes. Every trace in a life-of-a-query should be generated
   from a captured run, not transcribed.

2. **It contradicts the document's own promise.** § 8 shows the validation ladder passing on
   suppression, unit, currency, period, entity, measure, and SEC concept. For the wrong answer,
   *all of those pass too* — entity is Apple, unit is USD, period is FY2023, the concept exists and
   is reported. The `measure` check cannot distinguish "revenue" from "contract with customer
   liability revenue recognized", so validation admits it and the deterministic renderer states it
   with full confidence. The document's central claim is that validation is what protects
   correctness; its own example is a case where validation is structurally satisfied and the answer
   is still wrong.

3. **It is the oldest known weakness in this system, not a new one.** Sibling-measure collision is
   what motivated the descriptor work in the first place — `Revenues` and 990 `Total Revenue` once
   scored within 0.001 of each other. That was fixed for *cross-source* confusion. This shows it is
   unfixed for *within-source* sibling concepts, where every structural dimension agrees.

### What to do about it

Do not simply swap in the phrasing that works. That hides the finding. Either:

- **use the failing question as the example it actually is** — trace it as a case where structural
  validation passes and the answer is still wrong, and say plainly that measure disambiguation
  within a source is an open boundary; or
- **use a question that is reproducible**, and add the failure to § *The queryability boundary* as a
  fourth category: *semantically ambiguous* — the operation exists, the record exists, the
  structural checks pass, and the measure named by the user does not uniquely identify a concept.

The second is probably right for launch, with the first as a short subsection. A fourth boundary
category is a stronger document than three, and this system has earned the right to name it.

## Structural notes

**The eleven shape sections are thinner than the point trace, and they are the reason to read on.**
§§ *point* through *topical* mostly restate the mechanism table in prose. What makes a "life of X"
worth reading is the branch that only X takes — the concept 404 for a backtracked point lookup, the
sentinel for Census, the EIN ambiguity for a traversal, the pre-fetch refusal for a ranking. Three
fully traced lives with real values beat eleven summaries.

**Order the shape sections by what they prove, not by the planner's enum.** Refused, backtracked and
suppressed should come first: they are short, they carry the thesis, and they show behaviour a
warehouse cannot produce. The happy paths can follow.

**One factual gap.** § 6 says a rejected `Attempt` "remains in the trace." True in the object model —
worth stating that the debugger surfaces it, since the ability to see the rejected branches is the
only way a reader can distinguish a correct answer from a plausible one. For Search nobody needs the
plan, because it is always the same plan. Here it is the product.

## Recommendation

Land it after the headline example is resolved. The architecture description is accurate, the
boundary section is the strongest argument in the repository, and the honesty about weaker paths is
a feature. The example is the one thing a reader will try for themselves.
