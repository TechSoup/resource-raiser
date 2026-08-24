# Revision record: *The life of a query*

This records five draft–critique–revision passes. The critiques deliberately use the standard set
in the prior code reviews: prefer concrete traces over architecture prose, verify implementation
claims in code, treat refusal as behavior rather than failure, and test whether each abstraction
earns its weight.

## Iteration 1 — The architecture outline

**Draft.** A common lifecycle followed by one section for every query shape.

**Claude-style critique.** Accurate, but it reads like a table of contents for a design document.
Nothing has a name, value, score, identifier, or returned record. Calling it a “life” is premature:
the reader sees stages without seeing a request live through them. Fetching is particularly easy to
lose between planning and validation.

**Revision.** Anchor the document on the observed Apple FY2023 revenue query. Show its actual
`QueryIntent`, resolved identity, SEC concept, raw returned value, admitted evidence, and rendered
answer. Make “fetch the fact” a separate stage.

## Iteration 2 — One concrete but linear trace

**Draft.** The Apple query now provides a strong narrative spine.

**Claude-style critique.** Better, but the trace accidentally implies that every query follows the
same linear path. Resource Raiser is interesting precisely because point, ranking, derivation,
correlation, topical search, and graph traversal do not share one execution mechanism. The document
still describes a pipeline with retries rather than a decision graph.

**Revision.** Put the branch diagram before the trace. Separate query *shape* from query
*mechanism*. After the common lifecycle, add a mechanism table and a “life of…” section for each
implemented shape. Describe grant traversal as a specialized route rather than pretending it is a
generic planner shape.

## Iteration 3 — Branching without a hard boundary

**Draft.** The shape-specific lives make the control structure visible.

**Claude-style critique.** The draft still sounds as though branching eventually finds an answer.
It underplays the most distinctive behavior: some query classes cannot be expressed by the
available APIs. A keyed nonprofit lookup cannot rank the nonprofit population. Search results are
not a population. Two facts at incompatible grains cannot be correlated. These are not edge-case
errors; they define the query system.

**Revision.** Add the queryability boundary immediately after the common trace. Define the source
APIs as a query algebra. Add “the life of an impossible query” with structural impossibility,
empirical unavailability, and incompatible composition. Treat refusal as a successful plan and
connect the boundary to the NoSQL access-path analogy.

## Iteration 4 — Audit the claims against the code

**Draft.** The conceptual structure is now complete.

**Claude-style critique.** Several statements could be aspirational. Is lexicographic source
ordering implemented? Are assumptions actually editable? Is a native-series mechanism present, or
does the current planner fan out by period? Is grant relationship a real value in `SHAPES`? A launch
document should not smooth over distinctions the code makes.

**Revision.** Verify claims against `planner.py`, `harness.py`, `domain.py`, `connectors.py`, and
`renderers.py`. State the current mechanisms exactly:

- eleven generic shapes from `planner.SHAPES`;
- exact keyed operations for point/status/entity-list/topical;
- fan-out for comparison and timeseries;
- scan/order for population shapes;
- generate-and-test only for existential filtered subsets with complete per-entity reads;
- derive and materialize-and-correlate for compositions;
- specialized grant direction selection outside the generic shape tuple;
- lexicographic `(exact, completeness, discovery position)` ordering;
- real assumption overrides in `discover()`.

The audit also found gaps the conceptual draft had incorrectly described as solved. Comparison
fan-out does not yet prove unit/period compatibility; the optimized timeseries loop does not admit
Evidence per observation; ratios calculate and attach alignment warnings instead of refusing;
correlation enforces county grain and shared keys but not period/unit compatibility and defaults to
California when no state is resolved. Put these limitations in the document. Remove fabricated
timings and avoid claiming a source-native series mechanism is currently chosen.

## Iteration 5 — Edit for argument, not inventory

**Draft.** Factually careful, concrete, and comprehensive—but long and at risk of becoming a
catalog of shapes.

**Claude-style critique.** The reader needs one argument to survive the detail. Repetition should
do useful work: every shape section should answer the same three questions—what access path it
requires, what branch it takes, and what makes it impossible. Cross-cutting normalization,
validation, and backtracking should be explained once rather than rediscovered in every section.

**Revision.** Keep Apple as the full trace; make later shape sections compact branch narratives.
Use a single queryability table as the index. End with how new capabilities enlarge the answerable
query algebra and one compact thesis that includes fetching and refusal. The result is
`LIFE_OF_A_QUERY.md`.
