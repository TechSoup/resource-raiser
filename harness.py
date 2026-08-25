#!/usr/bin/env python3
"""Query harness — the orchestrator a skill delegates to.

  question
    -> ARD Agent Finder (POST /search via ard_client)  : which data source/table?
    -> retrieve from that source (live)                : SEC concept, or any OKF source
    -> synthesize a cited answer

Run as a CLI or as a server the connectors skill calls:
  set -a; source ./set_keys.sh; set +a
  python3 harness.py "How much did Apple spend on R&D in 2023?"     # one-shot (prints JSON)
  python3 harness.py --serve [--port 8099]                          # POST /ask {"question": ...}
"""
import os, sys, json, time, math, re, signal, traceback, urllib.parse, queue
import driver, ard_client, planner, store, llm, nlweb, connectors, renderers, runtime, docpage
from domain import Attempt, Clarification, ClarificationOption, Evidence, QueryIntent
from core import Toolkit

ROOT = os.path.dirname(os.path.abspath(__file__))
TK = Toolkit()

# Live-progress channel. When a request is streaming (/ask_stream), the handler installs a writer
# that pushes each event to THAT browser; otherwise _say is a no-op.
#
# PER-THREAD, not module-global. The server is ThreadingHTTPServer (see serve()), so requests run
# concurrently: with one global writer a second request overwrote the first's channel (its browser
# went silent), and worse, whichever request finished first cleared the global and silenced any
# request still running — which hung the UI (the client's reader loop only ends on a clean close)
# and cascaded, one stuck request killing every retry. Thread-local gives each request its own.
import threading
_EMIT = threading.local()
_EMIT_LOCK = threading.Lock()          # parallel executors emit from worker threads; serialize the writes


def _say(kind, **data):
    runtime.check()
    cb = getattr(_EMIT, "cb", None)
    if cb:
        try:
            with _EMIT_LOCK:
                cb({"kind": kind, **data})
        except Exception:
            pass


def _with_emitter(fn):
    """Bind the CALLING thread's stream writer onto a callable run on a worker thread. The fan-out
    helpers hand work to a ThreadPoolExecutor, and a fresh pool thread has no thread-local writer —
    so without this every event from a parallel stage is dropped. Always assign (even None): pool
    threads are reused, so a stale writer would otherwise publish into an already-closed socket.

    The question's usage ledger rides along for the same reason: a fan-out stage can issue most of
    the LLM calls, and they would go uncounted on a pool thread that never had the ledger bound."""
    cb = getattr(_EMIT, "cb", None)
    led = llm.ledger()
    disc = ard_client.usage()
    cancel, deadline = runtime.capture()

    def wrapped(*a, **k):
        _EMIT.cb = cb
        llm.bind_ledger(led)
        ard_client.bind_usage(disc)
        runtime.bind(cancel, deadline)
        return fn(*a, **k)

    return wrapped


import glob as _glob


def _source_types():
    out = {}
    for p in _glob.glob(os.path.join(ROOT, "sources", "*", "_access.md")):
        fm = driver.frontmatter(p)
        if fm.get("entityType"):
            out[os.path.basename(os.path.dirname(p))] = fm["entityType"]
    return out


SOURCE_TYPES = _source_types()

# illustrative example queries per source (homepage copy; the query engine is not driven by these)
SOURCE_EXAMPLES = {
    "sec-edgar": ["What was Apple's total revenue?", "Microsoft net income in 2023",
                  "Apple's diluted earnings per share"],
    "treasury": ["What is the US national debt?", "Euro to dollar exchange rate"],
    "census": ["Median household income in California", "Poverty rate in Chicago",
               "Unemployment rate in Detroit"],
    "cdc-places": ["Diabetes prevalence in Chicago", "Obesity rate in Miami"],
    "nonprofit-990": ["American Red Cross total revenue", "Is the Sierra Club a 501(c)(3)?"],
    "usaspending": ["How much federal funding has the American Red Cross received?"],
    "nih-reporter": ["How much NIH research funding does Stanford get?"],
    "nsf-awards": ["NSF research awards for MIT"],
    "grants-gov": ["What grants can a nonprofit apply for in education?"],
}
_SOURCE_ORDER = ["sec-edgar", "sec-bq", "treasury", "census", "cdc-places", "nonprofit-990", "nonprofit-bmf", "irs-990-bq", "census-acs-bq",
                 "irs-grants", "nonprofit-profile", "usaspending", "nih-reporter", "nsf-awards", "grants-gov", "college-scorecard", "fema"]

# Example questions grouped into themes for the homepage tab bar (interactive entry point).
EXAMPLE_TABS = [
    {"label": "🏛️ Nonprofits",
     "dirs": ["nonprofit-990", "nonprofit-bmf", "nonprofit-profile", "usaspending", "grants-gov", "nih-reporter", "nsf-awards"],
     "queries": [
        "What was the American Red Cross total revenue?",
        "Is the Sierra Club a 501(c)(3)?",
        "Where is the Nature Conservancy headquartered?",
        "What sector does Feeding America work in?",
        "Are donations to the ACLU Foundation tax-deductible?",
        "When was the Sierra Club founded?",
        "Who is the CEO of the Wikimedia Foundation?",
        "What does Feeding America do?",
        "How much federal funding has the American Red Cross received?",
        "How much NIH research funding does St. Jude receive?",
        "What grants can a nonprofit apply for in education?",
        "How much does the ACLU pay its officers?",
        "Is the American Red Cross in good standing with the IRS?",
        {"q": "Compare the total revenue of the American Red Cross and Feeding America",
         "tag": "comparison · 2 lookups"},
        {"q": "What share of the American Red Cross revenue comes from federal funding?",
         "tag": "ratio · cross-source join"},
        {"q": "What is the largest nonprofit in the US?", "tag": "ranking · BigQuery SQL"}]},
    {"label": "🎓 Academia & Research", "dirs": ["nih-reporter", "nsf-awards", "nonprofit-990", "college-scorecard"], "queries": [
        "What is the out-of-state tuition at Stanford?",
        "How many students attend Ohio State University?",
        "What is the admission rate at MIT?",
        "How much NIH research funding does Stanford get?",
        "NSF research awards for MIT",
        "How much NIH funding does Johns Hopkins receive?",
        "Harvard University total revenue",
        "NSF research awards for Caltech",
        {"q": "Does Harvard or MIT get more NIH funding?", "tag": "comparison · 2 lookups"},
        {"q": "Give me some universities that get more than a billion dollars from NIH",
         "tag": "filtered-subset · propose-and-verify"},
        {"q": "Which university gets the most NIH funding?", "tag": "refused · no source can rank"}]},
    {"label": "📈 Companies", "dirs": ["sec-edgar"], "queries": [
        "What was Apple's total revenue?",
        "Microsoft net income in 2023",
        "Apple's diluted earnings per share",
        "Tesla's research and development expense",
        "NVIDIA total revenue",
        {"q": "What were Apple's earnings?", "tag": "ambiguous · answered per interpretation"},
        {"q": "How big is Microsoft?", "tag": "ambiguous · answered per interpretation"}]},
    {"label": "🏘️ Communities & Health", "dirs": ["census", "cdc-places", "fema"], "queries": [
        "What percentage of households have broadband internet in Detroit?",
        "What disasters have been declared in California?",
        "What percentage of households receive SNAP in Detroit?",
        "What is the median rent in Miami?",
        "What is the homeownership rate in Houston?",
        "Median household income in California",
        "Poverty rate in Chicago",
        "Diabetes prevalence in Chicago",
        "Obesity rate in Miami",
        "Unemployment rate in Detroit",
        {"q": "Which city has the highest diabetes rate?", "tag": "ranking · server-ordered"},
        {"q": "Which cities have a diabetes rate above 20%?", "tag": "filtered-subset · threshold"},
        {"q": "Across California counties, is median household income correlated with diabetes rates?",
         "tag": "correlation · materialized"}]},
    {"label": "🏦 Government & Money", "dirs": ["treasury", "usaspending"], "queries": [
        "What is the US national debt?",
        "Euro to dollar exchange rate",
        "Japanese yen to dollar exchange rate"]},
    {"label": "💰 Grants & Funding", "dirs": ["grants-gov", "usaspending", "nih-reporter", "nsf-awards"], "queries": [
        "What grants can a nonprofit apply for in education?",
        "How much federal funding has Feeding America received?",
        "How much federal funding has Habitat for Humanity received?",
        "What grants are available for medical research?",
        {"q": "Which organization receives the most federal funding?", "tag": "ranking · server-ordered"},
        {"q": "How much NIH research funding does Johns Hopkins receive?", "tag": "entity-list · fully paged"}]},
    # Shape-driven examples. Each is TAGGED with the query shape the planner picks, so the
    # contrast is visible: which are one call, which fan out, and which are honestly refused.
    # Every query here has been verified end-to-end.
    {"label": "🧭 Query shapes", "dirs": ["cdc-places", "census", "usaspending", "nih-reporter",
                                          "nonprofit-990", "nsf-awards", "sec-edgar"],
     "queries": [
        {"q": "What was the American Red Cross total revenue?", "tag": "point"},
        {"q": "Is the Sierra Club a 501(c)(3)?", "tag": "status"},
        {"q": "NSF research awards for Caltech", "tag": "entity-list"},
        {"q": "Does Harvard or MIT get more NIH funding?", "tag": "comparison · 2 lookups"},
        {"q": "Compare the total revenue of the American Red Cross and Feeding America",
         "tag": "comparison · 2 lookups"},
        {"q": "Which city has the highest diabetes rate?", "tag": "ranking · server-ordered"},
        {"q": "Which organization receives the most federal funding?", "tag": "ranking · server-ordered"},
        {"q": "Which cities have a diabetes rate above 20%?", "tag": "filtered-subset · threshold"},
        {"q": "Give me some universities that get more than a billion dollars from NIH",
         "tag": "filtered-subset · propose-and-verify"},
        {"q": "What share of the American Red Cross revenue comes from federal funding?",
         "tag": "ratio · cross-source join"},
        {"q": "Across California counties, is median household income correlated with diabetes rates?",
         "tag": "correlation · materialized"},
        {"q": "Which university gets the most NIH funding?", "tag": "refused · no source can rank"},
        {"q": "Which nonprofit has the highest revenue?", "tag": "ranking · BigQuery SQL"},
        {"q": "Which nonprofits have revenue over 10 billion dollars?", "tag": "filtered-subset · BigQuery"},
        {"q": "What were Apple's earnings?", "tag": "ambiguous · answered per interpretation"},
     ]},
]


# Curated TechSoup view — the data organized around what TechSoup and its nonprofit/library/
# foundation customers actually need: validate an org, close the digital divide, understand a
# nonprofit's finances, read the communities it serves, find funding.
TECHSOUP_TABS = [
    {"label": "✅ Validate a nonprofit", "dirs": ["nonprofit-990", "nonprofit-bmf", "nonprofit-profile"],
     "queries": [
        "Is the American Red Cross a 501(c)(3)?",
        "Is Feeding America in good standing with the IRS?",
        "Are donations to the Sierra Club tax-deductible?",
        "What sector does Habitat for Humanity work in?",
        "Where is the Nature Conservancy headquartered?",
        "When did the Wikimedia Foundation become tax-exempt?"]},
    {"label": "🖥️ Digital divide", "dirs": ["census", "cdc-places"], "queries": [
        "What percentage of households have broadband internet in Detroit?",
        "Computer ownership rate in Chicago",
        "What percentage of households have broadband in Mississippi?",
        {"q": "Across California counties, is median household income correlated with diabetes rates?",
         "tag": "correlation · materialized"}]},
    {"label": "💰 Nonprofit finances", "dirs": ["nonprofit-990", "usaspending"], "queries": [
        "What was the American Red Cross total revenue?",
        "How much does the ACLU pay its officers?",
        "How much federal funding has Feeding America received?",
        {"q": "What share of the American Red Cross revenue comes from federal funding?",
         "tag": "ratio · cross-source"},
        {"q": "Compare the total revenue of the American Red Cross and Feeding America",
         "tag": "comparison"}]},
    {"label": "🍎 Communities served", "dirs": ["census", "cdc-places", "fema"], "queries": [
        "What percentage of households receive SNAP in Detroit?",
        "What is the median rent in Miami?",
        "What is the homeownership rate in Houston?",
        "Poverty rate in Chicago",
        "Diabetes prevalence in Chicago",
        "What disasters have been declared in California?"]},
    {"label": "🎓 Funding & grants", "dirs": ["grants-gov", "usaspending", "nih-reporter", "nsf-awards"],
     "queries": [
        "What grants can a nonprofit apply for in education?",
        "How much federal funding has the American Red Cross received?",
        "How much NIH research funding does Stanford get?",
        "NSF research awards for MIT"]},
    {"label": "🏫 Higher education", "dirs": ["college-scorecard"], "queries": [
        "What is the out-of-state tuition at Stanford?",
        "How many students attend Ohio State University?",
        "What is the admission rate at MIT?",
        "What is the graduation rate at UCLA?"]},
]


def _source_categories():
    """Every theme (category) a source dir belongs to — a source spanning categories lists them all."""
    m = {}
    for t in EXAMPLE_TABS:
        for d in t.get("dirs", []):
            m.setdefault(d, []).append(t["label"])
    return m


def _sources_catalog():
    """The data sources, read live from each source's OKF `_access.md`: name, what it covers
    (entityType), how many tables/leaves it exposes, the categories it spans, and example queries."""
    cats = _source_categories()
    out = []
    for p in _glob.glob(os.path.join(ROOT, "sources", "*", "_access.md")):
        d = os.path.basename(os.path.dirname(p))
        fm = driver.frontmatter(p) or {}
        count = len([f for f in _glob.glob(os.path.join(ROOT, "sources", d, "*.md"))
                     if os.path.basename(f) != "_access.md"])
        out.append({"dir": d, "name": (fm.get("title") or d).replace(" (access)", ""),
                    "covers": fm.get("entityType", ""), "count": count,
                    "categories": cats.get(d, []), "examples": SOURCE_EXAMPLES.get(d, [])})
    out.sort(key=lambda s: _SOURCE_ORDER.index(s["dir"]) if s["dir"] in _SOURCE_ORDER else 99)
    return out


_POP_SHAPES = ("ranking", "aggregate", "filtered-subset", "correlation")


def _normalize_shape(ctx):
    """Deterministic sanity pass over the LLM's shape, correcting self-contradictory classifications
    WITHOUT another model call. Shape classification is the softest link; these guards catch the
    common ways it slips, so a mislabel degrades gracefully instead of executing the wrong plan."""
    shape = ctx.get("shape")
    # A ranking says "which COUNTY / NONPROFIT / COMPANY has the most X" — the unit noun is the
    # POPULATION, not a named entity. The classifier sometimes puts it in `entity`, which would make
    # the population→point downgrade below misfire; scrub those generic nouns first.
    _POP_NOUNS = {"county", "counties", "city", "cities", "state", "states", "place", "places",
                  "nonprofit", "nonprofits", "charity", "charities", "organization", "organizations",
                  "company", "companies", "university", "universities", "college", "colleges",
                  "school", "schools", "recipient", "recipients"}
    if (ctx.get("entity") or "").strip().lower() in _POP_NOUNS:
        ctx["entity"] = ""
    ents = [e for e in (ctx.get("entities") or []) if e]
    periods = [p for p in (ctx.get("periods") or []) if p]
    ent = ctx.get("entity")

    # a population shape has NO named entity by definition; if one was extracted, it was misread
    if shape in _POP_SHAPES and ent:
        ctx["shape"] = "comparison" if len(ents) >= 2 else "point"
    # comparison needs >= 2 named entities
    elif shape == "comparison" and len(ents) < 2:
        ctx["shape"] = "point" if ent or len(ents) == 1 else "point"
        if len(ents) == 1 and not ent:
            ctx["entity"] = ents[0]
    # timeseries needs >= 2 periods
    elif shape == "timeseries" and len(periods) < 2:
        ctx["shape"] = "point"
    # a filtered-subset with no threshold and not existential is really a ranking
    elif shape == "filtered-subset" and not (ctx.get("threshold") or {}).get("value") \
            and ctx.get("quantifier") != "existential":
        ctx["shape"] = "ranking"
    if ctx.get("shape") != shape:
        _say("status", icon="🔧", msg=f"Reclassified {shape} → {ctx['shape']} (shape sanity check)")
    return ctx


def discover(question, sites=None, assumptions=None):
    """Pass 1: extract the ENTITY, the entity-expunged ATTRIBUTE, and (via the entity's
    TYPE) which SOURCE(s) apply. Pass 2: match the attribute to fields within those sources."""
    _say("status", icon="🔍", msg="Reading your question…")
    src_list = "\n".join(f"- {d}: covers {t}" for d, t in SOURCE_TYPES.items())
    ctx = json.loads(TK.llm(
        "A demographic or population restriction ('for Asian residents', 'for women', 'among "
        "adults 18-64', 'for renters') is part of the ATTRIBUTE, never part of the entity. The "
        "entity is only the named company, nonprofit, place or organization: in 'unemployment "
        "rate for Asian residents in Texas' the entity is 'Texas' and the attribute is "
        "'unemployment rate for Asian residents'. Putting the restriction in the entity loses it "
        "- retrieval runs on the attribute, so the general measure is returned instead.\n"
        "Analyze a data question. Return JSON with: 'entity' (the single company/nonprofit/place/org "
        "it is about, or empty), 'entities' (ALL named entities if it compares several, else []), "
        "'type' (one of: company, nonprofit, place, org, none), 'attribute' (the metric/measure asked, "
        "with the entity REMOVED — e.g. 'total revenue', 'poverty rate'), 'period' ('FY<year>' or "
        "'latest'), 'periods' (list of 'FY<year>' if it spans several years, else []), 'sources' (the "
        "dir names below whose entity type + scope fit), and 'shape', exactly one of:\n"
        "  point           - ONE specific measured value ('Apple's total revenue', 'euro to dollar "
        "exchange rate', 'the US national debt'). A currency exchange rate or a single national figure "
        "is 'point' even with no named organization or place — it is NOT 'topical'.\n"
        "  status          - one named entity, a yes/no or category ('Is X a 501(c)(3)?')\n"
        "  entity-list     - one named entity, the records belonging to it ('NSF awards for MIT')\n"
        "  comparison      - TWO OR MORE NAMED entities compared ('Harvard or MIT — more NIH funding?')\n"
        "  timeseries      - one named entity across several periods ('Apple revenue 2019-2024')\n"
        "  ranking         - which member of an OPEN population is highest/top-N, entities NOT named "
        "('which university gets the most NIH funding', 'largest nonprofit')\n"
        "  aggregate       - one statistic over an OPEN population ('how many 501(c)(3)s are there')\n"
        "  filtered-subset - members of a population matching a numeric THRESHOLD ('nonprofits over $1M', "
        "'universities getting more than a billion from NIH', 'cities above a 20% diabetes rate')\n"
        "  ratio           - TWO OR MORE MEASURES combined, usually for ONE entity and often from "
        "DIFFERENT sources: a share/fraction/percent-of, a ratio, or one measure set against another "
        "('what share of X's revenue is federal funding', 'X's revenue vs the federal funding it "
        "receives', 'NIH dollars per resident')\n"
        "  topical         - no entity; a topic or keyword ('grants for education')\n"
        "  correlation     - is measure A RELATED to / associated with measure B across a population "
        "('is poverty correlated with diabetes across counties', 'do richer counties have less obesity')\n"
        "KEY DISTINCTION 2: 'comparison' compares the SAME measure across DIFFERENT named entities; "
        "'ratio' combines DIFFERENT measures (usually of one entity). 'Red Cross vs Feeding America "
        "revenue' is comparison; 'Red Cross revenue vs its federal funding' is ratio.\n"
        "KEY DISTINCTION: if the entities being compared are NAMED in the question it is 'comparison'; "
        "if the question asks the engine to find them from a whole population it is 'ranking' (top/most) "
        "or 'filtered-subset' (a stated numeric cut-off).\n"
        "Also return 'threshold': for a filtered-subset, {\"op\": \">\"|\">=\"|\"<\"|\"<=\", \"value\": <number "
        "as a plain integer, e.g. a billion = 1000000000, 20 percent = 20>}, else null.\n"
        "Also return 'quantifier': 'existential' if the question asks only for EXAMPLES ('give me some', "
        "'a few', 'name some', 'examples of'), or 'exhaustive' if it asks which/all members qualify.\n"
        "Also return 'interpretations': whenever the MEASURE is genuinely ambiguous — it could mean "
        "several materially DIFFERENT things a careful analyst would not conflate — a list of the 2-4 "
        "distinct specific measures it could mean (each a concrete attribute string, entity removed). "
        "These words are ALWAYS ambiguous, so ALWAYS populate interpretations for them:\n"
        "  'earnings' / 'profit' / 'profits' -> ['net income','operating income','EBITDA','gross profit']\n"
        "  'how big is X' / 'size of X' -> ['total revenue','total assets','number of employees','net income']\n"
        "  'performance' -> ['total revenue','net income','diluted earnings per share']\n"
        "Return [] ONLY for a measure that is already precise ('total revenue', 'net income', 'poverty "
        "rate', 'diabetes rate') — do NOT invent ambiguity for a specific measure.\n"
        "SOURCES:\n" + src_list, question, json_mode=True, stage="classify"))
    ctx = _normalize_shape(ctx)
    if assumptions:
        allowed = {"entity", "type", "attribute", "period", "shape", "concept"}
        applied = {k: v for k, v in assumptions.items() if k in allowed and v not in (None, "")}
        ctx.update(applied)
        # A measure supplied on a follow-up is the user's answer to our clarification. Retaining
        # the classifier's original interpretations would immediately ask the same question again.
        if "attribute" in applied:
            ctx["interpretations"] = []
        ctx = _normalize_shape(ctx)
    # Robustness: the classifier sometimes drops the place from a "<measure> in <Place>" question
    # (leaving an empty entity). Recover it from the question so a place lookup doesn't fail with no geo.
    if ctx.get("type") == "place" and not (ctx.get("entity") or "").strip():
        # "in" was the only preposition here, so "the population OF Colorado" had no safety net:
        # the classifier dropped the entity, nothing recovered it, and every candidate then failed
        # with "no geo" until the attempt budget ran out.
        recovered = _recover_place(question)
        if recovered:
            ctx["entity"] = recovered
    sources = [s for s in (ctx.get("sources") or []) if s in SOURCE_TYPES] or list(SOURCE_TYPES)
    sources = _ensure_grant_graph(question, sources)
    if sites:
        # An explicit NLWeb `site=` is the caller stating the corpus. That outranks the
        # classifier's guess — the point of the parameter is to constrain, not to suggest.
        wanted = [s for s in sites if s in SOURCE_TYPES]
        if wanted:
            sources = wanted
    # A per-entity question (about ONE named entity) must never be confined to population-only sources
    # (the BigQuery *-bq sources answer rankings/aggregates, not a single point lookup). If the
    # classifier picked only those, widen to all sources so the per-entity source is discoverable.
    if (ctx.get("shape") in ("point", "status", "entity-list", "comparison", "timeseries")
            and ctx.get("entity") and all(s.endswith("-bq") for s in sources)):
        sources = list(SOURCE_TYPES)
    _say("plan", entity=ctx.get("entity") or "", type=ctx.get("type") or "none",
         attribute=ctx.get("attribute") or "", period=ctx.get("period") or "latest", sources=sources)
    # Choose the routing text by whether the entity is a KEY or a DIMENSION. A company/place/nonprofit
    # is resolved to a fetch key (CIK/FIPS/EIN), so it's expunged and the ATTRIBUTE ranks the metric
    # cleanly ("total revenue", not "Apple total revenue"). A currency, security type, etc. (type
    # 'none'/'org') is NOT a key — it's the leaf's own discriminator, so the FULL question must rank it
    # (otherwise 'euro' is lost among ~30 identical exchange-rate leaves). The other search is appended
    # as a fallback; backtracking + the answer check settle the rest.
    resolvable = ctx.get("type") in ("company", "nonprofit", "place")
    primary = (ctx.get("attribute") or question) if resolvable else question
    secondary = question if resolvable else (ctx.get("attribute") or question)
    _say("status", icon="📚", msg="Asking the ARD Agent Finder which data tables can answer this…")
    seen, hits = set(), []
    # Attribute-only and full-question retrieval are complementary views, not two separately billed
    # ranking tasks. The finder embeds both in one provider call, unions by max similarity, and runs
    # one rerank over the shared candidate pool.
    try:
        found = ard_client.search_many([primary, secondary], k=12, sources=sources,
                                       rerank_query=primary)
    except ard_client.DiscoveryError as e:
        raise SystemExit(str(e)) from e
    for h in found:
        if h["identifier"] not in seen:
            seen.add(h["identifier"])
            hits.append(h)
    _say("candidates", items=[{"title": h["title"], "score": h["score"], "publisher": h.get("publisher")}
                              for h in hits[:6]])
    # The finder has already had the LLM score each table's declared subject and scope. Its
    # score floor is the eligibility decision; asking a second LLM here made valid queries
    # intermittently disappear (notably the four interpretations of "How big is Microsoft?").
    return ctx, hits


# The philanthropic grant graph (IRS 990: who funds whom) and the FEDERAL grant sources
# (grants.gov opportunities, USAspending awards) share the word "grant", and the classifier
# picks between them by wording alone. "Which states receive the most grant dollars" reads as
# federal to it about as often as philanthropic — same prompt, same model, different answer run
# to run. When the wording is clearly about the 990 grant graph, put irs-grants in the candidate
# pool rather than leave it to chance. This WIDENS the pool, it does not override the classifier:
# discovery and the planner still choose, so a genuinely federal question is unaffected.
_GRANT_GRAPH_RE = re.compile(
    r"\bgrant graph\b|\bgrantmaker|\bgrant-?making\b|\bwho funds\b|\bfoundations? (that )?fund\b"
    r"|\bgrants? (made|received|given)\b|\bgrant dollars\b|\bgrant money\b|\bbiggest (recipients|funders)\b"
    r"|\bphilanthrop", re.I)


def _ensure_grant_graph(question, sources):
    if _GRANT_GRAPH_RE.search(question or "") and "irs-grants" not in sources:
        return sources + ["irs-grants"]
    return sources


# --- ARD entry browsing --------------------------------------------------------------------
# The demo's claim is that discovery is a SERVICE — so browsing the registry goes through the ARD
# API (GET /agents, GET /agents/entry, POST /explore) exactly as searching it does. An earlier cut
# read registry/meta.json directly from here; it worked, but it quietly made the browser a special
# case that reached around the very interface being demonstrated.


def _ard_list(source, page=1, per=50, q=""):
    """One page of catalog entries. ARD paginates by opaque pageToken; the UI wants page numbers,
    so walk tokens forward to the requested page — cheap at these sizes, and it keeps the client
    honest about the cursor being opaque."""
    per = max(1, min(per, 100))                       # the spec caps pageSize at 100
    token, page = "", max(1, page)
    for _ in range(page - 1):
        d = ard_client.agents(publisher=source, q=q, page_size=per, page_token=token)
        token = d.get("pageToken")
        if not token:
            break
    d = ard_client.agents(publisher=source, q=q, page_size=per, page_token=token or "")
    total = d.get("totalSize", 0)
    return {"source": source, "total": total, "page": page,
            "pages": max(1, (total + per - 1) // per), "per": per, "query": q,
            "pageToken": d.get("pageToken"),
            "entries": [{"identifier": e["identifier"], "title": e.get("displayName", ""),
                         "description": e.get("description", ""), "scope": e.get("scope", ""),
                         "queries": (e.get("representativeQueries") or [])[:4]}
                        for e in d.get("entries", [])]}


def _ard_entry(identifier):
    """The ARD catalog entry, passed through UNCHANGED under `ard_entry`.

    The browser shows the actual JSON the registry serves rather than a summary of it — for a demo
    about a discovery protocol, a hand-picked subset of the fields is the least interesting thing
    to look at."""
    e = ard_client.entry(identifier)
    if not e:
        return None
    return {"identifier": e["identifier"], "source": e.get("publisher", ""),
            "ard_entry": e,                                   # verbatim, every field
            "raw": (e.get("data") or {}).get("content", ""),
            "access_doc": e.get("accessDescriptor", "")}


def _ard_publishers():
    """Facet counts from POST /explore — what the source picker is built from."""
    f = (ard_client.explore("publisher").get("facets") or {}).get("publisher") or {}
    return [{"dir": b["value"], "count": b["count"]} for b in f.get("buckets", [])]


def _recover_place(question):
    """The place named at the end of a question, or None.

    The classifier intermittently returns an empty entity for a place question. Only "in" was
    matched here, so "the population OF Colorado" had no safety net: every candidate then failed
    with "no geo" until the attempt budget ran out.
    """
    m = re.search(r"\b(?:in|of|for|across|throughout) (?:the )?([A-Z][\w .,'&-]+?)\s*\??$",
                  question)
    return m.group(1).strip() if m else None


def _geo_from_fips(keys):
    if keys.get("fips_place"):                                # "SS-PPPPP" or "SSPPPPP"
        # Wikidata carries both spellings - Detroit is "26-22000", Miami is "1245000". Accepting
        # only the dashed one silently fell through to the county, so a question about Miami was
        # answered for Miami-Dade County and still said "Miami".
        v = "".join(ch for ch in keys["fips_place"] if ch.isdigit())
        if len(v) == 7:
            return f"place:{v[2:]}&in=state:{v[:2]}"
    if keys.get("fips_county"):                               # "SSCCC" or "SS-CCC"
        v = "".join(ch for ch in keys["fips_county"] if ch.isdigit())
        if len(v) == 5:
            return f"county:{v[2:]}&in=state:{v[:2]}"
    if keys.get("fips_state"):
        return f"state:{keys['fips_state']}"
    return None


def _place_levels(canon):
    """Ordered granularity alternatives for a place (most specific first) to backtrack through."""
    if not canon:
        return []
    import resolver
    try:
        return resolver.hierarchy(canon["qid"])
    except Exception:
        return [{"label": canon.get("label"), "keys": canon.get("keys", {})}]


_STATE_FIPS = {
    "alabama": "01", "alaska": "02", "arizona": "04", "arkansas": "05", "california": "06",
    "colorado": "08", "connecticut": "09", "delaware": "10", "district of columbia": "11",
    "washington dc": "11", "florida": "12", "georgia": "13", "hawaii": "15", "idaho": "16",
    "illinois": "17", "indiana": "18", "iowa": "19", "kansas": "20", "kentucky": "21",
    "louisiana": "22", "maine": "23", "maryland": "24", "massachusetts": "25", "michigan": "26",
    "minnesota": "27", "mississippi": "28", "missouri": "29", "montana": "30", "nebraska": "31",
    "nevada": "32", "new hampshire": "33", "new jersey": "34", "new mexico": "35", "new york": "36",
    "north carolina": "37", "north dakota": "38", "ohio": "39", "oklahoma": "40", "oregon": "41",
    "pennsylvania": "42", "rhode island": "44", "south carolina": "45", "south dakota": "46",
    "tennessee": "47", "texas": "48", "utah": "49", "vermont": "50", "virginia": "51",
    "washington": "53", "west virginia": "54", "wisconsin": "55", "wyoming": "56"}


def _resolve_geo(place):
    # US states resolve DETERMINISTICALLY (a fixed 50-value lookup) — no LLM/Wikidata, so a bare
    # state ("median household income in Michigan") never fails under concurrent load.
    fips = _STATE_FIPS.get((place or "").strip().lower().lstrip("the ").strip())
    if fips:
        return f"state:{fips}"
    return json.loads(TK.llm(
        "Convert this US place to a Census geography clause. State FIPS e.g. CA=06, NY=36, TX=48, "
        "FL=12, IL=17, WA=53, MA=25, PA=42, OH=39, GA=13, NC=37, AZ=04. A state -> 'state:NN'; a "
        'county -> "county:CCC&in=state:NN". JSON {"geo":"..."}.', place, json_mode=True)).get("geo")


class Backtrack(Exception):
    pass


class Prune(Backtrack):
    """Abandon every remaining option BELOW a named choice, not just this leaf.

    A verdict can be about the choice itself rather than the combination that produced it.
    "This table measures poverty, not broadband" is true for every entity, key and period
    under that table, so retrying them re-asks a question already answered. Raising
    Prune("hit") makes the solver advance the `hit` choice instead of its descendants.
    """

    def __init__(self, step, reason):
        super().__init__(reason)
        self.step = step


def _solve(steps, goal, state, i=0):
    """General depth-first backtracking search. `steps` = [(name, options_fn), ...] where
    options_fn(state) yields the ranked candidate values for that choice; `goal(state)` attempts
    the result and raises Backtrack on failure. Any dead-end backtracks to the next option of the
    deepest still-open choice. This is the ONE mechanism for every choice point."""
    if i == len(steps):
        return goal(state)
    name, options_fn = steps[i]
    last = None
    for opt in options_fn(state):
        try:
            return _solve(steps, goal, {**state, name: opt}, i + 1)
        except Prune as e:
            # Only the choice the verdict was about consumes it; deeper levels pass it up
            # rather than trying their remaining options against a settled dead end.
            if e.step != name:
                raise
            last = e
        except Backtrack as e:
            last = e
    raise Backtrack(f"no viable {name} ({last})")


# Per-process memo caches for values that are IDENTICAL across every backtrack attempt of one
# question — the ticker for a mention, and the resolved entity candidates. Without these the harness
# re-runs the same LLM + Wikidata calls dozens of times while exhausting candidates (the capex case).
_TICKER_CACHE = {}
_ENTITY_CACHE = {}


# What an entity IS, from Wikidata. Identifier families are strong evidence when present,
# but most Wikidata items carry none - "University of Detroit Mercy" has no EIN - so absence
# of identifiers cannot be read as absence of a type. P31 (instance of) is the evidence that
# actually discriminates, and it is matched on the CLASS LABEL rather than a curated QID set,
# because the set of classes meaning "a place" is large, open, and changes without notice.
_TYPE_KEYS = {
    # No gnis here: the Geographic Names system also names universities, hospitals and
    # stadiums, so a GNIS code does not distinguish a place from a building in one.
    "place": ("fips_place", "fips_county", "fips_state"),
    "company": ("cik", "ticker", "lei"),
    "nonprofit": ("ein",),
}

# Word sets for reading a Wikidata class label. Matched with word boundaries and checked in
# order, because a substring test on the joined labels is wrong in both directions: "country
# music group" contains "country", and "state" appears in far more than states. NOT_A_TYPE is
# checked first so a creative work, a person's name, a taxon or a sports club is excluded
# before any word inside it can be mistaken for a place.
_NOT_A_TYPE = ("album", "song", "single", "film", "movie", "video game", "game", "episode",
               "series", "version", "edition", "translation", "taxon", "species", "genus",
               "given name", "family name", "surname", "band", "group", "duo", "team", "club",
               "franchise", "brand name", "unisex name")

_CLASS_WORDS = {
    "nonprofit": ("nonprofit", "non-profit", "charity", "foundation", "university", "college",
                  "school", "museum", "hospital", "institute", "association", "society",
                  "organization", "organisation", "church", "educational"),
    "company": ("business", "company", "corporation", "enterprise", "manufacturer"),
    # No "country": it appears in "country music group". A nation still reads as a place via
    # "sovereign state". These are checked BEFORE the negative list so "island group" and
    # "university town" stay places while "musical group" does not.
    "place": ("city", "town", "village", "municipality", "county", "state", "borough",
              "settlement", "census-designated", "township", "district", "region",
              "territory", "capital", "metropolis", "commune", "prefecture", "province",
              "nation", "seat", "community", "colonia", "island", "locality", "hamlet",
              "populated place", "neighborhood", "neighbourhood", "suburb"),
}


def _kind_from_classes(labels):
    """Which of our types the Wikidata classes describe, or None when they describe none."""
    text = " ".join(str(l).lower() for l in labels).strip()
    if not text:
        return None
    def says(words):
        return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)
    # Place first: a class naming a kind of settlement is a place even when it also contains a
    # word like "town" that appears in "university town", or "group" in "island group".
    if says(_CLASS_WORDS["place"]):
        return "place"
    if says(_NOT_A_TYPE):
        return "other"
    for kind in ("nonprofit", "company"):
        if says(_CLASS_WORDS[kind]):
            return kind
    return "other"


def _type_compatible(keys, thint, class_labels=()):
    """False when the evidence says this candidate is not the kind of thing asked for.

    Order matters. Identifiers of the requested kind accept immediately. Otherwise P31 class
    labels decide, because they are present for effectively every item while identifiers are
    not: "University of Detroit Mercy" carries no EIN at all, so an identifier-only rule let
    it answer a question about the city of Detroit. Only when there is no evidence of any
    kind does the candidate pass, since absence is not disqualifying.
    """
    wanted = _TYPE_KEYS.get(thint)
    if not wanted:
        return True
    if keys and any(keys.get(k) for k in wanted):
        return True
    kind = _kind_from_classes(class_labels)
    if kind is not None:
        return kind == thint
    if keys and any(keys.get(k) for other, ks in _TYPE_KEYS.items() if other != thint for k in ks):
        return False
    return True


def _entity_options(mention, thint):
    """CHOICE: which canonical entity. Ranked (LLM-disambiguated) candidates, then None = native name."""
    if not mention:
        return [None]
    if (mention, thint) in _ENTITY_CACHE:                    # resolve a mention once per question, not per hit
        return _ENTITY_CACHE[(mention, thint)]
    _say("status", icon="🧩", msg=f"Resolving “{mention}” to a canonical entity…")
    import resolver
    try:
        cands = resolver._search(mention)
    except Exception:
        return [None]
    if not cands:
        return [None]
    listing = "\n".join(f"{i}. {c.get('label','')} — {c.get('description','')}" for i, c in enumerate(cands))
    try:
        order = json.loads(TK.llm(
            f'Rank the candidates by how well each is the {thint or "entity"} named "{mention}", best first. '
            'JSON {"order":[<indices>]}.\n' + listing, mention, json_mode=True,
            stage="resolve-entity")).get("order", [])
    except Exception:
        order = []
    order = [i for i in order if isinstance(i, int) and 0 <= i < len(cands)] or list(range(len(cands)))
    out = []
    for i in order[:3]:
        try:
            label, keys = resolver._claims(cands[i]["id"])
        except Exception:
            continue
        try:
            classes = list(resolver.class_labels(resolver.instance_of(cands[i]["id"])).values())
        except Exception:
            classes = []
        if not _type_compatible(keys, thint, classes):
            # "Detroit Institute of Arts" is a real Wikidata match for the mention
            # "Detroit" and the ranker will sometimes keep it. A nonprofit cannot answer a
            # question about a place, so this is settled deterministically from the resolved
            # identifiers rather than left to the ranking.
            _say("status", icon="🚫",
                 msg=f"“{label}” is not a {thint} — not a candidate for this question")
            continue
        out.append({"qid": cands[i]["id"], "label": label, "keys": keys})
    if out:
        _say("resolve", mention=mention, label=out[0].get("label") or mention, keys=out[0].get("keys") or {})
    _ENTITY_CACHE[(mention, thint)] = res = out + [None]
    return res


def _key_options(state, ctx):
    """CHOICE: which key/granularity for the chosen (field, entity), by source shape.
    (Place granularity place->county->state is just one instance of this general choice.)"""
    fm = driver.frontmatter(state["hit"]["identifier"])
    keys = (state["entity"] or {}).get("keys", {})
    mention = ctx.get("entity") or ""
    if fm.get("concept"):
        return ([str(int(keys["cik"]))] if keys.get("cik") else []) + [None]        # CIK, then native ticker
    if fm.get("field") or fm.get("classification") or fm.get("bmf"):
        return ([keys["ein"].replace("-", "")] if keys.get("ein") else []) + ([mention] if mention else []) or [None]
    if fm.get("profile"):                                                        # Wikidata QID keyed
        return ([(state["entity"] or {}).get("qid")] if (state["entity"] or {}).get("qid") else []) or [None]
    if fm.get("scorecard"):                                                       # school name (API self-matches)
        return ([mention] if mention else []) or [None]
    if fm.get("fema"):                                                            # US state (from place resolution)
        st = [l["keys"].get("fips_state") for l in _place_levels(state["entity"]) if l["keys"].get("fips_state")]
        return (st + ([mention] if mention else [])) or [None]
    if fm.get("variable"):
        opts = [g for g in (_geo_from_fips(l["keys"]) for l in _place_levels(state["entity"])) if g]
        return opts + (["__native__"] if mention else []) or [None]                 # place -> county -> state
    if fm.get("measureid"):
        opts = [l["label"].replace(" County", "").strip() for l in _place_levels(state["entity"]) if l.get("label")]
        return opts or ([mention.replace(" County", "").strip()] if mention else [None])
    if fm.get("search", {}).get("want") == "organization":
        # Name-matched source: try the CANONICAL resolved name first ("Caltech" -> "California
        # Institute of Technology"), then the raw mention. Backtracking walks them in order.
        label = (state["entity"] or {}).get("label")
        return list(dict.fromkeys([n for n in (label, mention) if n])) or [None]
    return [None]                                                                   # treasury / keyword search


_AMOUNT_KEYS = ("fundsObligatedAmt", "estimatedTotalAmt", "Award Amount", "award_amount",
                "total_obligated", "awardCeiling")


def _dig(obj, path):
    """Read a possibly-nested field ('organization.org_name') out of a record."""
    for p in (path or "").split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(p)
    return obj


def _amount(r):
    for k in _AMOUNT_KEYS:
        v = r.get(k)
        if v not in (None, ""):
            try:
                return float(str(v).replace(",", "").replace("$", ""))
            except ValueError:
                pass
    return 0.0


def _identity_scope(rows, ident):
    """A source that matches recipients by NAME rather than a canonical key (declared as
    `identity.match: name` in its OKF doc) can silently span several separately registered
    organizations — local chapters, affiliates, or merely similarly-named entities. Group the
    rows by the identity the source itself reports and state the scope, instead of presenting
    the sum as one organization's figure. Generic: no source-specific knowledge here."""
    if ident.get("match") != "name" or not rows:
        return {}
    groups = {}
    for r in rows:
        nm = str(_dig(r, ident.get("field") or "") or "(unidentified)")
        g = groups.setdefault(nm, {"name": nm, "count": 0, "total_usd": 0.0})
        g["count"] += 1
        g["total_usd"] += _amount(r)
    gl = sorted(groups.values(), key=lambda g: -g["total_usd"])
    out = {"match": "name", "matched_entities": len(gl), "entity_groups": gl}
    if len(gl) > 1:
        out["note"] = (f"Matched by NAME, not a canonical identifier: these rows span {len(gl)} "
                       f"separately registered recipients, so any total is across all of them.")
    return out


# --- fetch strategies -----------------------------------------------------------------------------
# One (field, entity, key, period) fetch attempt. Every source shape is a named strategy below, and
# _fetch dispatches to whichever one the source's OKF frontmatter declares (its marker key). Adding a
# source that fits an existing shape is data-only — a new sources/<name>/_access.md with the right
# marker, no code here. A genuinely new access pattern is one new handler + one _STRATEGIES entry; the
# per-attempt plumbing (identifier, key, period, entity) and the SystemExit->Backtrack wrapping are
# shared, so a handler contains only what is unique to that shape.
from collections import namedtuple
_F = namedtuple("_F", "fm ident key period attribute mention state ctx")


def _s_concept(f):
    """SEC EDGAR — an XBRL us-gaap concept per company. Resolves ticker->CIK once per mention."""
    if f.key:
        return driver.fetch_metric(f.attribute, cik=f.key, period=f.period, log=False,
                                   concept=f.ctx.get("concept"))
    if f.mention not in _TICKER_CACHE:                   # same mention on every backtrack — resolve once
        _TICKER_CACHE[f.mention] = json.loads(TK.llm(
            'JSON {"ticker":"<US stock ticker or empty>"}.', f.mention, json_mode=True, stage="resolve-entity")).get("ticker")
    ticker = _TICKER_CACHE[f.mention]
    if not ticker:
        raise Backtrack("no ticker")
    return driver.fetch_metric(f.attribute, ticker, f.period, log=False,
                               concept=f.ctx.get("concept"))


# Nonprofit sources resolve names authoritatively via ProPublica (EIN spine), so fall back to the NAME
# when the Wikidata candidate carries no EIN. Otherwise a candidate with no EIN (the real "Sierra Club",
# a c4) backtracks to a sibling that does (the c3 "Sierra Club Foundation"), and the answer flips.
# `key or mention` keeps a real EIN when present, else uses the name.
def _np_org(f):
    org = f.key or f.mention
    if not org:
        raise Backtrack("no nonprofit key")
    return org


def _s_classification(f):
    import nonprofit
    return nonprofit.classify(_np_org(f))


def _s_field(f):
    import nonprofit
    return nonprofit.fetch_np(f.fm["field"], _np_org(f), f.period)


def _s_bmf(f):
    import nonprofit
    return nonprofit.bmf(f.fm["bmf"], _np_org(f))


def _s_profile(f):
    import orgprofile as profile
    if not f.key:
        raise Backtrack("no wikidata qid")
    return profile.fetch(f.fm["profile"], f.key, (f.state.get("entity") or {}).get("label"))


def _s_scorecard(f):
    import college
    return college.fetch(f.fm["scorecard"], f.key or f.mention)


def _s_fema(f):
    import fema
    return fema.fetch(f.key or f.mention)


# --- generic point-lookup REST fetch, driven entirely by the source's OKF `fetch:` descriptor -------
# Census, CDC and Treasury are not special-cased: each declares a `fetch:` block in its _access.md
# (op, how to reach the row, how to map response cells/fields to the answer record), and _s_rest
# interprets it. Adding another point-lookup REST source is a new _access.md `fetch:` block — no code.
# (SEC/nonprofit/Wikidata/awards keep handlers because they RESOLVE ids and AGGREGATE, not merely
# template-fill — that algorithmic work is the "smart accessor", not something config can express.)
_FETCH_SPEC_CACHE = {}


def _fetch_spec(f):
    """The source's declarative fetch spec: the leaf's own `fetch:` if present, else the `fetch:` block
    of the _access.md it links to (cached — the block is shared by every leaf of the source)."""
    if f.fm.get("fetch"):
        return f.fm["fetch"]
    src = f.fm.get("source")
    if not src:
        return None
    path = os.path.normpath(os.path.join(os.path.dirname(f.ident), src))
    if path not in _FETCH_SPEC_CACHE:
        try:
            _FETCH_SPEC_CACHE[path] = driver.frontmatter(path).get("fetch")
        except Exception:
            _FETCH_SPEC_CACHE[path] = None
    return _FETCH_SPEC_CACHE[path]


def _bind_param(b, f):
    """Resolve an accessor-param binding. `$geo` = the entity's Census geography (native names resolved),
    `$key` = the resolved entity key, `~field` = a value pinned in the leaf frontmatter, else literal."""
    if b == "$geo":
        geo = _resolve_geo(f.mention) if f.key == "__native__" else f.key
        if not geo:
            raise Backtrack("no geo")
        return geo
    if b == "$key":
        if not f.key:
            raise Backtrack("no key")
        return f.key
    if isinstance(b, str) and b.startswith("~"):
        return f.fm.get(b[1:], "")
    return b


def _rows_of_resp(resp, rows_spec):
    if rows_spec in ("matrix", "objects", None):
        return resp
    obj = resp                                               # a dotted path into the response, e.g. "data"
    for part in str(rows_spec).split("."):
        obj = obj[int(part)] if part.lstrip("-").isdigit() else (obj or {}).get(part, [])
    return obj


def _pick_row(rows, pick):
    if not isinstance(rows, list):
        return None
    if pick == "index0":
        return rows[0] if rows else None
    if isinstance(pick, str) and pick.startswith("first:"):  # first object with a truthy field
        fld = pick.split(":", 1)[1]
        return next((r for r in rows if isinstance(r, dict) and r.get(fld)), None)
    return None


def _bind_field(b, f, resp, row):
    """Resolve one output-record field binding to a value (None => omit the field). `cell:r,c` reads a
    matrix response, `col:name` a picked object field, `col:~leaf` a field NAMED by a leaf value,
    `leaf:a,b` the first present leaf field, `title`/`title~suffix` the leaf title, `filterval` the
    Treasury filter's dimension value, `lit:x` a literal."""
    if not isinstance(b, str):
        return b
    if b.startswith("lit:"):
        return b[4:]
    if b == "title":
        return f.fm.get("title")
    if b.startswith("title~"):
        return (f.fm.get("title") or "").split(b[6:])[0]
    if b.startswith("cell:"):
        r, c = (int(x) for x in b[5:].split(","))
        return resp[r][c] if isinstance(resp, list) and len(resp) > r and len(resp[r]) > c else None
    if b.startswith("col:~"):
        return (row or {}).get(f.fm.get(b[5:]))
    if b.startswith("col:"):
        return (row or {}).get(b[4:])
    if b.startswith("leaf:"):
        return next((f.fm[n] for n in b[5:].split(",") if f.fm.get(n)), None)
    if b == "filterval":
        flt = f.fm.get("filter") or ""
        return flt.split(":eq:")[-1] if ":eq:" in flt else None
    return b


def _quirk_acs_pe(f, resp, rec):
    """ACS Data Profile quirk: for a PERCENT row the value lives in the *PE* column while the *E* column
    is -888888888 ("not applicable"). Read the percent sibling when the estimate is that sentinel — else
    a poverty/unemployment RATE looks like missing data and the search backtracks forever over a value
    that is simply in the other column. Also enforce the jam-sentinel = missing rule."""
    if not (isinstance(resp, list) and len(resp) >= 2):
        raise Backtrack("no census row")
    def jam(x):
        try:
            return float(x) <= -100000000                    # ACS jam sentinels are large negatives
        except (TypeError, ValueError):
            return False
    val, var = rec.get("value"), rec.get("variable")
    if str(val).strip() == "-888888888" and var.endswith("E") and not var.endswith("PE"):
        pe = var[:-1] + "PE"
        geo = _resolve_geo(f.mention) if f.key == "__native__" else f.key
        a2 = driver.accessor(f.ident, "acs", geo=geo, get=pe)
        if isinstance(a2, list) and len(a2) >= 2 and not jam(a2[1][1]):
            val, var = a2[1][1], pe
    if jam(val):
        raise Backtrack("jam null")
    rec["value"], rec["variable"] = val, var
    return rec


_QUIRKS = {"acs_pe": _quirk_acs_pe}


def _s_rest(f):
    """Execute a source's declarative `fetch:` spec — the ONE handler for every point-lookup REST
    source (census, CDC, treasury, and any future one). No source-specific code lives here."""
    spec = _fetch_spec(f)
    if not spec:
        raise Backtrack("no fetch spec for this source")
    params = {k: _bind_param(v, f) for k, v in (spec.get("params") or {}).items()}
    if spec.get("query"):                                    # build a query-string param (Treasury)
        q = re.sub(r"~(\w+)", lambda m: str(f.fm.get(m.group(1), "")), spec["query"])
        ff = spec.get("filter_field")
        if ff and f.fm.get(ff):
            q += f"&filter={f.fm[ff]}"
        params["query"] = q
    resp = driver.accessor(f.ident, spec.get("op", "get"), **params)
    rows = _rows_of_resp(resp, spec.get("rows"))
    row = _pick_row(rows, spec["pick"]) if spec.get("pick") else None
    if spec.get("pick") and row is None:
        raise Backtrack("no matching row")
    rec = {}
    for outkey, b in (spec.get("fields") or {}).items():
        v = _bind_field(b, f, resp, row)
        if v is not None:
            rec[outkey] = v
    if spec.get("quirk"):
        rec = _QUIRKS[spec["quirk"]](f, resp, rec)
    rec["source"] = spec.get("source")
    return rec


def _s_search(f):
    """Federal award/opportunity search (NSF/NIH/USAspending/grants.gov) — a keyword or org query,
    paged to completion when the source declares entity-scoped completeness."""
    s = f.fm["search"]
    val = (f.key or f.mention) if s["want"] == "organization" else f.attribute
    if not val:
        raise Backtrack("no search term")
    cap = (planner.capabilities(f.ident) or {}).get(s["operation"], {})
    page = cap.get("page") or {}

    def _pull(**extra):
        r = driver.accessor(f.ident, s["operation"], **{s["arg"]: val, **extra})
        for part in s["extract"].split("."):
            r = r[int(part)] if isinstance(r, list) else r.get(part, [])
        return r if isinstance(r, list) else []

    if page.get("complete_for") == "entity" and page.get("offset_param"):
        # ENTITY-scoped completeness: this org's own records fit under the offset ceiling, so page them
        # all. Without this the "total" is just the largest N projects — Johns Hopkins reads $208M
        # instead of $969M, and every threshold comparison is wrong.
        step, off, res = int(page.get("max") or 500), 0, []
        _say("status", icon="📄", msg=f"Paging every record for this organization…")
        while off < int((cap.get("population") or {}).get("ceiling") or 15000):
            chunk = _pull(**{page["offset_param"]: off})
            res.extend(chunk)
            if len(chunk) < step:
                break
            off += step
        _say("status", icon="📄", msg=f"{len(res)} records retrieved (complete for this organization)")
    else:
        res = _pull()
    out = {"query": val, "source": f.fm.get("title")}
    if isinstance(res, list):
        rows = [r for r in res if isinstance(r, dict)]
        total = sum(_amount(r) for r in rows)            # compute totals HERE, not in the LLM
        out["record_count"] = len(rows)
        if total:
            out["total_usd"] = round(total, 2)
            out["total_usd_display"] = "${:,.0f}".format(total)
        # Completeness is DECLARED, not guessed: a capped page is a partial total. Propagating it
        # matters most for joins — dividing a truncated numerator by a complete denominator is the
        # characteristic way a cross-source join produces a confident wrong number.
        out["complete"] = bool(page.get("complete")) or page.get("complete_for") == "entity"
        if not out["complete"]:
            out["coverage"] = (f"total is across the {len(rows)} award records returned by this "
                               f"query, not every award the organization has received")
        out.update(_identity_scope(rows, f.fm.get("identity") or {}))   # scope a name-matched result
        res = [{k: v for k, v in r.items() if not (isinstance(v, str) and len(v) > 240)}
               for r in rows][:8]                        # drop bulky prose (abstracts etc.)
    out["results"] = res
    return out


# The dispatch table: OKF marker key -> strategy. First marker the frontmatter declares wins, so the
# order here is only a tie-break (a leaf declares exactly one). To add a shape, append one pair — or,
# for a point-lookup REST source, add no code at all: give it a `fetch:` block and one of the markers
# routed to the generic _s_rest (variable/measureid/tfield are just its routing tags today).
_STRATEGIES = [
    ("concept", _s_concept), ("classification", _s_classification), ("field", _s_field),
    ("bmf", _s_bmf), ("profile", _s_profile), ("scorecard", _s_scorecard), ("fema", _s_fema),
    ("variable", _s_rest), ("measureid", _s_rest), ("tfield", _s_rest), ("search", _s_search),
]


def _fetch(state, ctx):
    """Attempt one complete (field, entity, key, period) assignment. Raise Backtrack on any failure.
    Dispatches to the strategy the source's OKF frontmatter declares — see _STRATEGIES."""
    identifier = state["hit"]["identifier"]
    fm = driver.frontmatter(identifier)
    f = _F(fm, identifier, state.get("key"), state.get("period") or "latest",
           ctx.get("attribute") or "", ctx.get("entity") or "", state, ctx)
    try:
        for marker, handler in _STRATEGIES:
            if fm.get(marker):
                return handler(f)
    except SystemExit as e:
        raise Backtrack(str(e))
    raise Backtrack("no structured retrieval for this source")


def _answers(question, data, structural=None):
    """Acceptance test at the goal of the search: is this record ABOUT the right thing for the question?
    This is a ROUTING check, not a fact-check — it turns backtracking from 'no data' into 'no WRONG
    data' by matching the record's qualifiers (measure, unit, currency, place/entity) to the question.

    It must NOT judge the value itself: the model's own world-knowledge is wrong about magnitudes,
    exchange-rate direction, and 'future' dates (its training cutoff makes recent data look fake), so
    letting it fact-check the number causes false rejections of correct answers. Fail-open on error."""
    if structural is not None:
        if not structural.accepted:
            return False, structural.reason
        if not structural.residual_semantic_check:
            return True, ""
    try:
        v = json.loads(TK.llm(  # acceptance check
            "You route data: decide whether the DATA record is ABOUT the right thing for the QUESTION. "
            "Accept when its MEASURE, UNIT, CURRENCY, and PLACE/ENTITY match what the question asks. "
            "Reject ONLY for a clear mismatch in one of those: a different measure (e.g. 'intragovernmental "
            "holdings' when the total national debt was asked), a wrong unit (a total amount when a "
            "per-share value or a percentage/rate was asked, or vice versa), a different named currency, or "
            "a different place/entity (a broader containing area used as a proxy for a place is fine). "
            "CRUCIAL: do NOT judge the numeric VALUE in any way — do not consider whether it seems too "
            "large or small, whether an exchange rate looks inverted, or whether a date is recent, old, or "
            "in the future. Treat the value and its date as authoritative and current. "
            "A NEGATIVE or FALSE answer is still an ANSWER: for a yes/no question, a record whose value is "
            "'no' / false / 0 (e.g. is_501c3=false correctly answers 'Is X a 501(c)(3)?' with NO) ANSWERS "
            "the question and MUST be accepted — never reject a record because the answer it gives is "
            "negative, or you will backtrack until you find a wrongly-positive match. Judge only WHAT the "
            "record is about. Bias strongly toward ACCEPT: if the record names the same currency, place, or "
            "measure the question asks about — even inside a longer official title (e.g. 'Treasury Reporting "
            "Rates of Exchange: Euro Zone-Euro' answers a euro exchange-rate question) — ACCEPT. Reject only "
            "when you are CONFIDENT it is a different currency/place/measure (e.g. China-Renminbi when the "
            'euro was asked). When in doubt, ACCEPT. Return JSON {"ok": true|false, "why": "<short reason>"}.',
            json.dumps({"question": question, "data": data}), json_mode=True, stage="check"))
        ok, why = bool(v.get("ok", True)), v.get("why", "")
        # BACKSTOP: never reject on a DATE/PERIOD mismatch. The period is handled by the fetch's own
        # backtracking (requested -> latest), and a period NEWER than the latest published data can
        # never be fetched — so rejecting here sends the search backtracking forever over an
        # unsatisfiable requirement (the FY2024-not-yet-filed loop). The prompt already forbids
        # date-judging; enforce it deterministically since the model sometimes does it anyway.
        if not ok and re.search(r"\b(fy\d*|fiscal|period|years?|dates?|20\d\d|recent|latest|current)\b",
                                why or "", re.I):
            return True, ""
        return ok, why
    except Exception:
        return True, ""


def _rows_of(res, cap):
    """Normalise a ranking/aggregate response into [{label, value}] using the operation's declared
    `returns` mapping. Handles dict rows (JSON objects) and positional rows (Census array-of-arrays)."""
    ret = cap.get("returns") or {}
    lab, val = ret.get("label"), ret.get("value")
    for part in str(ret.get("path") or "").split("."):
        if part and isinstance(res, dict):
            res = res.get(part) or []
    if not isinstance(res, list):
        return []
    out = []
    for r in res:
        if isinstance(r, dict):
            l, v = r.get(lab), r.get(val)
        elif isinstance(r, list):                       # positional (array-of-arrays)
            try:
                l, v = r[int(lab)], r[int(val)]
            except (ValueError, TypeError, IndexError):
                continue
        else:
            continue
        try:
            v = float(str(v).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            continue
        if l is None:
            continue
        out.append({"label": str(l), "value": v})
    return out


def _run_bq(question, ctx, p):
    """Population query answered by the BigQuery IRS-990 source (SQL over the whole population).
    Reached only when the planner selected a `bq:` leaf, which happens only when GOOGLE_CLOUD_PROJECT
    is set (see the credential gate in planner.capabilities)."""
    import bq
    fm = driver.frontmatter(p["hit"]["identifier"]) or {}
    cfg = fm["bq"]                                    # {table, field, entity_field, name_*, source}
    shape = ctx.get("shape")
    measure = cfg.get("field") or cfg.get("value_field") or "value"
    _say("status", icon="🗄️", msg=f"Running SQL over the whole population (BigQuery) by {measure}…")
    if shape == "aggregate":
        return bq.aggregate(cfg, "count")
    asc = any(w in question.lower() for w in ("lowest", "least", "smallest", "fewest", "bottom"))
    thr = ctx.get("threshold") if shape == "filtered-subset" else None
    return bq.rank(cfg, n=10, ascending=asc, threshold=thr)


# named-org lookup patterns — reverse (X is the RECIPIENT) vs forward (X is the FUNDER). These read
# the question directly, so they work even when the classifier drops the entity (which it sometimes does).
_REVERSE_RE = re.compile(r"\bwho\s+fund(s|ed)\b|\b(which|what)\s+(foundations?|charities?|funders?|"
                         r"nonprofits?|donors?|organizations?)\s+(fund|funded|support|back)\b|"
                         r"\bfunders?\s+of\b|\bwho\s+supports?\b|\bfunded\s+by\b", re.I)
_FORWARD_RE = re.compile(r"\bdoes\b.+\bfund\b|\bgrants?\b.+\b(made|make|gave|give)\b|\bmade\s+by\b|"
                         r"\brecipients?\s+of\b|\bgrantees?\s+of\b|\bhow\s+much\s+did\b.+\b(grant|give)\b", re.I)
_ENTITY_RE = [
    r"does\s+(?:the\s+)?(.+?)\s+(?:fund|support|give)",
    r"grants?\s+(?:did\s+)?(?:the\s+)?(.+?)\s+(?:make|made|give|gave|grant)",
    r"grants?\s+made\s+by\s+(?:the\s+)?(.+)", r"recipients?\s+of\s+(?:the\s+)?(.+)",
    r"grantees?\s+of\s+(?:the\s+)?(.+)", r"how\s+much\s+did\s+(?:the\s+)?(.+?)\s+(?:grant|give)",
    r"(?:foundations?|charities?|funders?|nonprofits?|donors?|organizations?)\s+"
    r"(?:fund|funded|support|back)\s+(?:the\s+)?(.+)",
    r"who\s+funds?\s+(?:the\s+)?(.+)", r"who\s+funded\s+(?:the\s+)?(.+)",
    r"funders?\s+of\s+(?:the\s+)?(.+)", r"who\s+supports?\s+(?:the\s+)?(.+)",
]


def _grant_entity(question, ctx):
    """The named org in a grant lookup — the classifier's entity, or extracted from the question
    when the classifier dropped it."""
    e = (ctx.get("entity") or "").strip()
    if e:
        return e
    q = question.strip().rstrip("?.")
    for p in _ENTITY_RE:
        m = re.search(p, q, re.I)
        if m and m.group(1).strip().lower() not in ("", "it", "they", "them", "this", "that"):
            return m.group(1).strip()
    return ""


def _grant_direction(question, ctx, grants):
    """Pick the grant-graph TRAVERSAL from the question, in code. Discovery only decides that this is
    a grant-graph question at all; distinguishing the near-identical leaves is more reliable done
    deterministically here than left to the LLM reranker. Precedence matters, and named-org lookups
    are detected by QUESTION PATTERN (not the classifier's entity, which is sometimes empty)."""
    ql = question.lower()
    ents = [e for e in (ctx.get("entities") or []) if e]
    states = grants.find_states(question)
    if len(ents) >= 2 and any(w in ql for w in ("same", "both", "common", "overlap", "shared")):
        return "shared"
    if len(states) >= 2 or "states" in ql or "state " in ql or " by state" in ql:
        return "geo"
    if _REVERSE_RE.search(ql):
        return "reverse"
    if _FORWARD_RE.search(ql):
        return "forward"
    # exploratory — no single named org
    major, _cw = grants.cause_of(ql)
    if ("what cause" in ql or "which cause" in ql or "by cause" in ql or "kinds of" in ql
            or (major and any(w in ql for w in ("how much", "goes to", "spent", "funding for",
                "grants for", "money for", "directed to", "support for", "given to")))):
        return "theme"
    if any(w in ql for w in ("in total", "total value", "total amount", "overall", "altogether",
                             "how many grant", "average grant", "how much grant money was", "total grant",
                             # the words people actually use to ask for the headline numbers — note
                             # "overview" itself was missing, so "give me an overview of the grant
                             # graph" fell through to the biggest-grantmakers ranking
                             "overview", "summary", "summarize", "big picture", "snapshot")):
        return "overview"
    if (ctx.get("threshold") or {}).get("value") is not None:
        return "ranking"                                          # funders_above (threshold branch)
    if any(w in ql for w in ("recipient", "receive", "funded by the most", "most funders",
                             "most foundations", "most different", "get the most", "gets the most")):
        return "biggest_recipients"
    return "ranking"                                              # biggest grantmakers


def _run_grants(question, ctx, p):
    """The IRS 990 GRANT GRAPH — who funds whom. Discovery routes grant-graph questions here; the
    TRAVERSAL is chosen deterministically from the question: named-entity lookups (forward/reverse),
    or EXPLORATORY queries over the whole graph — rankings of funders/recipients, graph patterns
    (shared grantees), geographic flows, aggregates, and threshold subsets. Local edge table."""
    import grants
    direction = _grant_direction(question, ctx, grants)
    ql = question.lower()
    asc = any(w in ql for w in ("lowest", "least", "smallest", "fewest", "bottom"))
    entity = (ctx.get("entity") or "").strip()

    if direction == "ranking":                                    # biggest grantmakers (funders)
        thr = ctx.get("threshold") if ctx.get("threshold") else None
        if thr and thr.get("value") is not None:
            _say("status", icon="🕸️", msg="Filtering grantmakers by total granted (grant graph)…")
            return grants.funders_above(thr["value"], ascending=str(thr.get("op", ">")).startswith("<"))
        _say("status", icon="🕸️", msg="Ranking grantmakers over the IRS 990 grant graph…")
        return grants.top_grantmakers(n=10, ascending=asc)
    if direction == "biggest_recipients":                         # ranking of recipients (in-degree)
        by = "funders" if any(w in ql for w in ("most funders", "most foundations", "most donors",
             "different funders", "different foundations", "how many funder")) else "dollars"
        _say("status", icon="🕸️", msg="Ranking grant recipients over the IRS 990 grant graph…")
        return grants.biggest_recipients(n=10, by=by, ascending=asc)
    if direction == "geo":                                        # money by place
        states = grants.find_states(question)
        if len(states) >= 2:
            _say("status", icon="🕸️", msg=f"Grant flow {states[0]} → {states[1]} (grant graph)…")
            return grants.geo("flow", from_state=states[0], to_state=states[1])
        mode = "funders" if any(w in ql for w in ("send", "sent", "sending", "give the most",
               "gives the most", "from which state", "which states give")) else "recipients"
        _say("status", icon="🕸️", msg="Ranking states by grant dollars (grant graph)…")
        return grants.geo(mode, ascending=asc)
    if direction == "overview":                                   # headline aggregates
        m = re.search(r"20(2[0-4])", question)
        _say("status", icon="🕸️", msg="Summarizing the IRS 990 grant graph…")
        return grants.overview(year=int(m.group(0)) if m else None)
    if direction == "theme":                                      # grants by cause (NTEE join)
        major, word = grants.cause_of(ql)
        grouped = any(w in ql for w in ("what cause", "which cause", "by cause", "kinds of", "breakdown"))
        _say("status", icon="🕸️", msg="Grouping grants by cause (IRS 990 grant graph × NTEE)…")
        return grants.grants_by_cause(None if grouped or not word else word)
    if direction == "shared":                                     # graph intersection (two funders)
        ents = [e for e in (ctx.get("entities") or []) if e] or ([entity] if entity else [])
        if len(ents) < 2:
            raise SystemExit("comparing shared grantees needs TWO named funders.")
        _say("status", icon="🕸️", msg=f"Finding organizations funded by both {ents[0]} and {ents[1]}…")
        return grants.shared_grantees(ents[0], ents[1])

    org = _grant_entity(question, ctx)                            # forward/reverse need one named org
    if not org:
        raise SystemExit("this grant question needs a named organization (a funder or a recipient).")
    if direction == "reverse":
        _say("status", icon="🕸️", msg=f"Tracing who funds {org} (IRS 990 grant graph)…")
        return grants.reverse(org)
    _say("status", icon="🕸️", msg=f"Tracing grants made by {org} (IRS 990 grant graph)…")
    return grants.forward(org)


def _run_ranking(question, ctx, p, top_n=10):
    """RANKING / AGGREGATE: one call to an operation the source DECLARED can see the whole
    population (server-side order, or a complete enumeration we order ourselves)."""
    hit, cap, op = p["hit"], p["capability"], p["operation"]
    fm = driver.frontmatter(hit["identifier"]) or {}
    acc_path = planner.access_path(hit["identifier"])
    url = (((driver.frontmatter(acc_path) or {}).get("access") or {}).get("operations") or {}).get(op, {})
    needed = {f for _, f, _, _ in __import__("string").Formatter().parse(url.get("url", "")) if f}
    params = {k: fm[k] for k in needed if k in fm}       # the leaf pins its own params (measureid, get…)
    thr = ctx.get("threshold") or {}
    if "n" in needed:                                   # a threshold needs a deeper slice to filter from
        params["n"] = 500 if thr.get("value") is not None else max(top_n, 25)
    for k in needed - set(params):                       # geography/partition placeholders
        if k in ("level", "fips", "geo"):
            params[k] = (ctx.get("partition") or {}).get(k) or ""
    _say("status", icon="📥", msg=f"Ranking the whole population via “{hit['title']}”…")
    rows = _rows_of(driver.accessor(hit["identifier"], op, **params), cap)
    if not rows:
        raise SystemExit(f"ranking returned no usable rows from {hit['title']}")
    if not (cap.get("order") or {}).get("server"):       # complete enumeration -> order it here
        rows.sort(key=lambda r: r["value"], reverse=True)
    asc = any(w in question.lower() for w in ("lowest", "least", "smallest", "fewest", "bottom"))
    if asc:
        rows = sorted(rows, key=lambda r: r["value"])
    scanned = len(rows)
    out = {"question": question, "source": fm.get("title") or hit["title"],
           "measure": (fm.get("title") or "").split(" — ")[0], "scanned": scanned, "complete": True}
    if thr.get("value") is not None:                    # FILTERED-SUBSET: apply the stated cut-off
        import operator
        cmp = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}.get(thr.get("op"), operator.gt)
        kept = [r for r in rows if cmp(r["value"], float(thr["value"]))]
        out.update({"threshold": f"{thr.get('op', '>')} {thr['value']}", "matches": len(kept),
                    "ranking": kept[:50]})
        # The scan is ordered by value, so a filter that does NOT fill the scan window has provably
        # found every match. If it saturates, more may lie beyond the window — a lower bound, say so.
        if kept and len(kept) >= scanned:
            out["complete"] = False
            out["note"] = (f"at least {len(kept)} — the {scanned}-row scan window filled up, so there "
                           f"may be more beyond it")
        elif not kept:
            out["note"] = f"no member of the population is {thr.get('op', '>')} {thr['value']}"
        return out
    rows = rows[:top_n]
    out.update({"ranking": rows, "top": rows[0]})
    return out


def _materialize(hit, grain="county", scope="06", say=None):
    """Fetch one measure at population grain and normalise it to spine-addressed observations.
    Driven entirely by the capability declaration (grain / entity_kind / enumerate), so a new
    source that declares the same things needs no code here."""
    ident = hit["identifier"]
    fm = driver.frontmatter(ident) or {}
    caps = planner.capabilities(ident)
    op, cap = next(((o, c) for o, c in caps.items() if c.get("grain") == grain), (None, None))
    if not op:
        raise SystemExit(f"{hit['title']} does not serve data at {grain} grain")
    vintage = str(fm.get("year") or fm.get("fy") or "latest")

    def build():
        if fm.get("get") or fm.get("variable"):                 # Census: fetch the DP variable for the whole
            def rows_for(get=None):                             # level in one geo=<grain>:* call
                kw = {"geo": f"{grain}:*&in=state:{scope}"}
                if get:
                    kw["get"] = get
                arr = driver.accessor(ident, op, **kw)
                out = []
                for row in (arr[1:] if isinstance(arr, list) and len(arr) > 1 else []):
                    try:
                        v = float(row[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if v <= -100000000:                          # ACS jam value = missing, not a number
                        continue
                    out.append({"entity": store.eid("fips", row[-2] + row[-1]), "entity_name": row[0],
                                "value": v, "source": fm.get("title")})
                return out
            obs, var = rows_for(), fm.get("get", "")
            if not obs and var.endswith("E") and not var.endswith("PE"):     # percent-row E is all jam -> PE
                obs = rows_for(var[:-1] + "PE")
            return obs, {"op": op, "grain": grain}
        res = driver.accessor(ident, op, **{k: fm[k] for k in ("measureid", "get", "key") if k in fm},
                              n=5000)                            # CDC: ordered scan of every place
        ef = cap.get("entity_field") or "locationid"
        ret = cap.get("returns") or {}
        obs = []
        for r in (res if isinstance(res, list) else []):
            try:
                v = float(r.get(ret.get("value") or "data_value"))
            except (TypeError, ValueError):
                continue
            if not r.get(ef):
                continue
            obs.append({"entity": store.eid("fips", r[ef]),
                        "entity_name": r.get(ret.get("label") or "locationname"),
                        "value": v, "source": fm.get("title")})
        return obs, {"op": op, "grain": grain}

    return store.ensure(ident, grain, vintage, build, say=say)


def _run_correlate(question, ctx, p):
    """CORRELATION: materialize both measures into the commons, align them on the spine, and
    compute the statistic LOCALLY. Materialization is cached, so the second question that touches
    either measure pays nothing — the commons accretes."""
    # A correlation has TWO independent measures, so each is discovered on its own. Ranking one
    # list for the whole question tends to return two variants of the same measure (or only one
    # side at all), which is what made a rephrasing fail.
    spec = json.loads(TK.llm(
        'Identify the TWO measures being related, and the population. Return JSON '
        '{"measure_a":"<measure only, no place>","measure_b":"<measure only, no place>",'
        '"grain":"county|state","state_fips":"<2-digit FIPS of the state mentioned, or empty for all>"}. '
        'e.g. "do richer counties have lower diabetes" -> measure_a "median household income", '
        'measure_b "diagnosed diabetes among adults".', question, json_mode=True))
    scope = re.sub(r"\D", "", str(spec.get("state_fips") or "")) or "06"
    picked, seen = [], set()
    for m in (spec.get("measure_a"), spec.get("measure_b")):
        if not m:
            continue
        for h in ard_client.search(m, k=6):
            if h["identifier"] in seen:
                continue
            if any(c.get("grain") == "county" for c in planner.capabilities(h["identifier"]).values()):
                seen.add(h["identifier"])
                picked.append(h)
                break
    if len(picked) < 2:
        raise SystemExit("a correlation needs two measures that are both available at county grain; "
                         f"found {len(picked)} for {spec.get('measure_a')!r} / {spec.get('measure_b')!r}")

    say = lambda m: _say("status", icon="🗃️", msg=m)
    series, meta = {}, []
    for h in picked:
        cap = next((c for c in planner.capabilities(h["identifier"]).values() if c.get("grain")), {})
        est = store.estimate(cap, "county", 3000)
        if est.get("known") and est["blowup"] and est["blowup"] > 50:
            raise SystemExit(f"materializing {h['title']} would transfer ~{est['rows']:,} rows for "
                             f"~3,000 counties (blowup {est['blowup']}x) — too expensive for one "
                             f"question; it should be materialized once per vintage instead")
        obs, cached = _materialize(h, scope=scope, say=say)
        if not obs:
            # a measure can exist yet be unusable at this grain (ACS suppresses many variables at
            # county level and returns jam values). Say WHICH measure failed, not just "0 matched".
            raise SystemExit(f"'{h['title']}' has no usable {'county'}-level values (all suppressed "
                             f"or missing), so it cannot be correlated at this grain")
        series[h["title"].split(" — ")[0][:40]] = obs
        meta.append({"measure": h["title"], "n": len(obs), "cached": cached})

    rows, report = store.align(series)
    labels = list(series)
    if len(rows) < 3:
        raise SystemExit(f"only {len(rows)} units had both measures — too few to correlate")
    xs = [r[labels[0]] for r in rows]
    ys = [r[labels[1]] for r in rows]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    sy = math.sqrt(sum((b - my) ** 2 for b in ys))
    r = (sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (sx * sy)) if sx and sy else 0.0
    return {"question": question, "correlation_r": round(r, 3), "n": n,
            "measures": labels, "series_meta": meta, "join": report,
            "source": " + ".join(dict.fromkeys(h["title"] for h in picked)),
            "caveats": [
                "correlation is not causation, and no confounders are controlled for",
                "this is an ECOLOGICAL correlation across counties — it says nothing about individuals",
                "county estimates are not independent samples (spatial autocorrelation), so the "
                "effective sample size is smaller than n",
            ]}


def _run_ambiguous(question, ctx):
    """The MEASURE is genuinely ambiguous ('earnings' = net income vs EBITDA vs EPS). Rather than
    silently pick one interpretation, answer EACH separately and let the reader choose — a wrong
    disambiguation is worse than several honest ones."""
    interps = [i for i in (ctx.get("interpretations") or []) if i][:4]
    ent = ctx.get("entity") or ""
    per = ctx.get("period") or "latest"
    yr = "" if per == "latest" else f" in {per}"
    _say("status", icon="🍃", msg=f"“{ctx.get('attribute')}” is ambiguous — answering "
                                 f"{len(interps)} interpretations in parallel: {', '.join(interps)}")

    def one(interp):
        sub = f"{interp} for {ent}{yr}" if ent else f"{interp}{yr}"
        try:
            r = retrieve_for(sub)
            d = r.get("data") or {}
            return {"interpretation": interp, "value": r.get("value"),
                    "label": d.get("metric") or d.get("measure") or interp,
                    "unit": d.get("unit"), "period": d.get("period"), "source": r.get("source"),
                    "concept": d.get("concept"), "source_identifier": r.get("source_identifier"),
                    "attempts": r.get("attempts") or []}
        except driver.SourceRateLimitError as e:
            return {"interpretation": interp, "value": None, "temporary_error": str(e)}
        except (SystemExit, Backtrack) as e:
            return {"interpretation": interp, "value": None, "error": str(e)}
        except Exception as e:
            return {"interpretation": interp, "value": None, "error": str(e)[:80]}

    # Run interpretations concurrently with a hard deadline. Some measures are genuinely
    # unavailable for the entity (a company has no clean employee-count concept) and backtrack
    # for a long time — the deadline stops one slow interpretation from blocking the rest.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ex = ThreadPoolExecutor(max_workers=min(4, len(interps)))
    futs = {ex.submit(_with_emitter(one), i): i for i in interps}
    answers, done = [], set()
    try:
        for fut in as_completed(futs, timeout=55):
            a = fut.result()
            answers.append(a)
            done.add(a["interpretation"])
            _say("status", icon="✅" if a.get("value") is not None else "↩️",
                 msg=f"{a['interpretation']}: {a.get('value') if a.get('value') is not None else 'no data'}")
    except Exception:                                        # as_completed timeout: take what finished
        pass
    for i in interps:
        if i not in done:
            answers.append({"interpretation": i, "value": None, "error": "timed out (measure likely unavailable)"})
    ex.shutdown(wait=False)                                   # abandon stragglers; don't block the response
    answers.sort(key=lambda a: interps.index(a["interpretation"]))
    temporary = next((a["temporary_error"] for a in answers if a.get("temporary_error")), None)
    if temporary:
        raise driver.SourceRateLimitError(temporary)
    got = [a for a in answers if isinstance(a.get("value"), (int, float))]
    if not got:
        raise SystemExit(f"'{ctx.get('attribute')}' is ambiguous and none of its interpretations "
                         f"({', '.join(interps)}) could be answered")
    return {"question": question, "ambiguous": True, "attribute": ctx.get("attribute"),
            "entity": ent, "interpretations": answers,
            "source": " · ".join(dict.fromkeys(a.get("source") or "?" for a in got))}


def _clarification(attribute, entity, raw_options):
    """Turn fetched alternatives into a resolvable, human-readable clarification.

    An embedding score collision is not enough: every option here has a returned value. Options
    that are effectively aliases for the same value and unit are collapsed before we interrupt a
    caller, because there is no useful decision for a human to make in that case.
    """
    options, seen = [], set()
    for i, raw in enumerate(raw_options or []):
        value = raw.get("value")
        if value is None:
            continue
        concept = raw.get("concept")
        assumption = raw.get("interpretation") or raw.get("metric") or raw.get("label") or attribute
        label = raw.get("label") or raw.get("metric") or assumption
        signature = (str(value), str(raw.get("unit") or "").lower(), str(raw.get("period") or ""))
        if signature in seen:
            continue
        seen.add(signature)
        option_id = concept or "measure:" + re.sub(r"[^a-z0-9]+", "-", str(assumption).lower()).strip("-")
        assumptions = {"measure": assumption}
        if concept:
            assumptions["concept"] = concept
        options.append(ClarificationOption(
            id=option_id or f"option-{i + 1}", label=str(label), value=value,
            unit=raw.get("unit"), period=raw.get("period"), source=raw.get("source"),
            concept=concept, assumptions=assumptions))
    if len(options) < 2:
        return None

    def materially_different(a, b):
        if str(a.unit or "").lower() != str(b.unit or "").lower():
            return True
        try:
            scale = max(abs(float(a.value)), abs(float(b.value)), 1.0)
            return abs(float(a.value) - float(b.value)) / scale >= 0.05
        except (TypeError, ValueError):
            return a.value != b.value

    if not any(materially_different(options[0], option) for option in options[1:]):
        return None
    subject = f" for {entity}" if entity else ""
    return Clarification(
        question=f"“{attribute}” has multiple materially different published meanings{subject}. Which one do you mean?",
        options=options[:4], attribute=attribute or "")


def _ambiguity_evidence(intent, hit, clarification, payload):
    sources = list(dict.fromkeys(o.source for o in clarification.options if o.source))
    return Evidence(kind="alternatives", source=" · ".join(sources) or hit.get("title") or "",
                    identifier=hit.get("identifier") or "", payload=payload,
                    entity={"label": intent.entity} if intent.entity else None,
                    measure=intent.measure,
                    provenance={"source_document": hit.get("identifier")},
                    warnings=["the requested measure has multiple materially different interpretations"])


def _ambiguity_result(question, ctx, hits, intent, clarification, on_ambiguity,
                      ledger, discovery, attempts=None):
    """Return Answer or Clarification from the same fetched alternatives."""
    hit = hits[0] if hits else {"identifier": "", "title": "", "publisher": ""}
    public_options = clarification.to_dict()["options"]
    payload = {"ambiguous": True, "attribute": clarification.attribute,
               "entity": ctx.get("entity") or "", "interpretations": public_options}
    evidence = _ambiguity_evidence(intent, hit, clarification, payload)
    source = {"identifier": hit.get("identifier"), "title": hit.get("title"),
              "publisher": hit.get("publisher")}
    base = {
        "question": question, "shape": intent.operation, "usage": ledger.snapshot(),
        "discovery_usage": discovery.snapshot(), "intent": intent.to_dict(),
        "attempts": attempts or [], "evidence": evidence.to_dict(),
        "source": source,
        "candidates": [{"identifier": h.get("identifier"), "title": h.get("title"),
                        "score": h.get("score"), "publisher": h.get("publisher")} for h in hits],
        "data": payload,
    }
    if on_ambiguity == "ask":
        return {**base, "status": "needs_clarification", "answer": None,
                "answer_renderer": None, "clarification": clarification.to_dict(),
                "plan": f"material ambiguity → ask the caller to choose among {len(public_options)} fetched values"}
    if on_ambiguity == "all":
        return {**base, "status": "answered", "answer_renderer": "alternatives",
                "answer": (f"“{clarification.attribute}” has {len(public_options)} materially different "
                           "interpretations; each fetched answer is shown below."),
                "plan": f"material ambiguity → {len(public_options)} interpretations answered separately"}

    # Non-interactive clients receive a usable answer plus every alternative in structured data.
    selected = clarification.options[0]
    point = Evidence(kind="point", source=selected.source or evidence.source,
                     identifier=hit.get("identifier") or "", payload={"value": selected.value},
                     entity={"label": intent.entity} if intent.entity else None,
                     measure=selected.assumptions.get("measure") or selected.label,
                     value=selected.value, unit=selected.unit,
                     currency="USD" if str(selected.unit or "").upper() == "USD" else None,
                     period=selected.period, warnings=evidence.warnings)
    rendered = renderers.render(point)
    answer = rendered.text if rendered else f"{selected.label}: {selected.value} {selected.unit or ''}".strip()
    payload["selected"] = selected.id
    return {**base, "status": "answered", "answer": answer,
            "answer_renderer": rendered.renderer if rendered else "dominant-interpretation",
            "evidence": point.to_dict(),
            "plan": f"material ambiguity → answer the preferred interpretation and expose {len(public_options) - 1} alternatives"}


def _run_derive(question, ctx, p):
    """CROSS-SOURCE JOIN (`ratio` / derived): decompose into independent sub-facts, fetch each
    through the full discover->resolve->fetch path, then COMPUTE IN THE ENGINE.

    Two properties matter here. (1) The arithmetic is done in code, never by the model — an LLM
    asked to total these got $10,000,000 against an actual $8,404,737 earlier in this project.
    (2) Joining figures that are not comparable is the characteristic way a join goes silently
    wrong, so periods and completeness are checked and reported rather than assumed."""
    spec = json.loads(TK.llm(
        "Decompose this into the INDEPENDENT figures needed, each a self-contained sub-question "
        "naming its entity and measure explicitly (they are answered separately, so they cannot "
        "refer to each other). Return JSON {\"parts\":[{\"label\":\"<short name>\",\"question\":\"<sub-question>\"}], "
        "\"compute\":\"share|ratio|difference|sum\", \"of\":\"<label of the numerator/left>\", "
        "\"per\":\"<label of the denominator/right>\"}. Use 'share' for 'what fraction/percent of X is Y'.",
        question, json_mode=True))
    parts = [p_ for p_ in (spec.get("parts") or []) if p_.get("question")][:4]
    if len(parts) < 2:
        raise SystemExit("could not decompose this into two or more figures to join")
    _say("status", icon="🔗", msg=f"Join: {len(parts)} independent figures — "
                                 + ", ".join(p_["label"] for p_ in parts))
    got = {}
    for part in parts:
        try:
            r = retrieve_for(part["question"])
            d = r.get("data") or {}
            got[part["label"]] = {"label": part["label"], "question": part["question"],
                                  "value": r.get("value"), "source": r.get("source"),
                                  "period": d.get("period") or d.get("as_of") or d.get("fiscal_year"),
                                  "complete": d.get("complete", True),
                                  "matched_entities": d.get("matched_entities"),
                                  "coverage": d.get("coverage")}
            _say("status", icon="✅", msg=f"{part['label']}: {r.get('value')} ({r.get('source')})")
        except SystemExit as e:
            got[part["label"]] = {"label": part["label"], "value": None, "error": str(e)}
            _say("status", icon="↩️", msg=f"{part['label']}: no data ({e})")
    vals = [g for g in got.values() if isinstance(g.get("value"), (int, float))]
    if len(vals) < 2:
        raise SystemExit("could not retrieve two comparable figures for this join")

    a, b = got.get(spec.get("of")), got.get(spec.get("per"))
    if not (a and b and isinstance(a.get("value"), (int, float)) and isinstance(b.get("value"), (int, float))):
        a, b = vals[0], vals[1]
    op, out = (spec.get("compute") or "ratio"), {}
    if op in ("share", "ratio") and b["value"]:
        r = a["value"] / b["value"]
        out = {"computed": round(r * 100, 2) if op == "share" else round(r, 4),
               "unit": "percent" if op == "share" else "ratio",
               "formula": f"{a['label']} / {b['label']} = {a['value']:,.0f} / {b['value']:,.0f}"}
    elif op == "difference":
        out = {"computed": round(a["value"] - b["value"], 2), "unit": "difference",
               "formula": f"{a['label']} - {b['label']} = {a['value']:,.0f} - {b['value']:,.0f}"}
    else:
        out = {"computed": round(sum(v["value"] for v in vals), 2), "unit": "sum",
               "formula": " + ".join(f"{v['label']}" for v in vals)}

    # JOIN ALIGNMENT: figures from different sources are routinely on different periods or
    # completeness footings. Combining them anyway is how a join produces a confident wrong number.
    warns = []
    periods = {v["label"]: v.get("period") for v in vals if v.get("period")}
    if len(set(periods.values())) > 1:
        warns.append("the figures cover different periods (" +
                     ", ".join(f"{k}: {v}" for k, v in periods.items()) + ")")
    for v in vals:
        if v.get("complete") is False:
            warns.append(f"'{v['label']}' is a PARTIAL total ({v.get('coverage') or 'capped result'}), "
                         f"so the result understates the true figure")
        if (v.get("matched_entities") or 1) > 1:
            warns.append(f"'{v['label']}' was matched by NAME across {v['matched_entities']} separately "
                         f"registered entities, so it is not the same legal entity as the other figure")
    if len({v.get("source") for v in vals}) < 2:
        warns.append("both figures came from the same source — this is not a cross-source join")
    return {"question": question, "join": [got[k] for k in got], "compute": op,
            "source": " + ".join(dict.fromkeys(v.get("source") or "?" for v in vals)),
            **out, **({"alignment_warnings": warns} if warns else {})}


def _run_generate_test(question, ctx, p, want=6):
    """EXISTENTIAL filtered-subset ("give me SOME universities over $1B"): the model PROPOSES
    candidates, the data VERIFIES each. This is the one plan where part of the answer originates
    outside the sources, so provenance is split explicitly and the result never claims completeness:
    membership is model-proposed, every reported VALUE is fetched and checked."""
    thr = ctx.get("threshold") or {}
    attr = ctx.get("attribute") or ""
    pop = ctx.get("population_type") or "organizations"
    cands = json.loads(TK.llm(
        f'Name up to {want + 4} real US {pop} MOST LIKELY to satisfy: "{attr} {thr.get("op", ">")} '
        f'{thr.get("value")}". Use each one\'s full official name (not an abbreviation). '
        'Return JSON {"candidates": ["...", ...]}.', question, json_mode=True)).get("candidates", [])
    cands = [c for c in cands if isinstance(c, str)][:want + 4]
    if not cands:
        raise SystemExit("could not propose candidates to test")
    _say("status", icon="🎯", msg=f"Proposing {len(cands)} candidates, then verifying each against the data")
    import operator
    cmp = {">": operator.gt, ">=": operator.ge, "<": operator.lt,
           "<=": operator.le}.get(thr.get("op"), operator.gt)
    tested, passing = [], []
    for c in cands:
        if len(passing) >= want:
            break
        try:
            r = retrieve_for(f"{attr} for {c}")
            v = r.get("value")
            ok = isinstance(v, (int, float)) and thr.get("value") is not None and cmp(v, float(thr["value"]))
            tested.append({"label": c, "value": v, "passes": bool(ok)})
            _say("status", icon="✅" if ok else "↩️",
                 msg=f"{c}: {v}{' — qualifies' if ok else ' — does not qualify'}")
            if ok:
                passing.append({"label": c, "value": v})
        except SystemExit as e:
            tested.append({"label": c, "value": None, "error": str(e)})
            _say("status", icon="↩️", msg=f"{c}: no data ({e})")
    return {"question": question, "ranking": passing, "matches": len(passing),
            "tested": tested, "threshold": f"{thr.get('op', '>')} {thr.get('value')}",
            "complete": False, "candidate_source": "model-proposed, then verified against the source",
            "note": ("these are EXAMPLES that were checked against the data, not the complete set — "
                     "candidates were proposed by the model, so qualifying members it did not think "
                     "of are missing"),
            "source": (tested[0].get("source") if tested else None) or p["hit"]["title"]}


def _run_fanout(question, ctx, shape):
    """COMPARISON / TIMESERIES: the answer is K ordinary lookups plus a comparison. Needs no
    population capability at all — which is why a keyed source can compare but cannot rank.
    Each sub-question goes through the full discover->resolve->fetch path via retrieve_for."""
    attr = ctx.get("attribute") or ""
    if shape == "timeseries":
        # Resolve the leaf + entity + key ONCE, then re-fetch per period. Re-running full
        # discovery for every year is what made this exceed ten minutes: each cycle re-ranks
        # and, for SEC, probes up to 25 candidate concepts.
        yrs = [y for y in (ctx.get("periods") or []) if y][:20]
        ent = ctx.get("entity") or ""
        if len(yrs) < 2:
            raise SystemExit("timeseries needs at least two periods")
        _say("status", icon="🧮", msg=f"Plan: resolve once, then read {len(yrs)} periods in parallel "
                                     f"({', '.join(str(y) for y in yrs)})")
        _c, _h, hit0, _t, _d, state0 = _search(f"{attr} for {ent}")

        def one_year(y):
            # per-YEAR fetch (not one concept for all): a filer legitimately reports different concepts
            # in different years (Apple: Revenues -> ASC-606), so each year picks the concept it used.
            try:
                d = _fetch({**state0, "period": str(y)}, ctx)
                return {"label": str(y), "value": d.get("value", d.get("value_usd", d.get("total_usd"))),
                        "source": hit0["title"]}
            except (Backtrack, SystemExit) as e:
                return {"label": str(y), "value": None, "error": str(e)}

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(6, len(yrs))) as ex:
            series = list(ex.map(_with_emitter(one_year), yrs))  # ex.map preserves input order
        for s in series:
            _say("status", icon="✅" if s.get("value") is not None else "↩️",
                 msg=f"{s['label']}: {s.get('value') if s.get('value') is not None else 'no data'}")
        got = [s for s in series if isinstance(s.get("value"), (int, float))]
        if len(got) < 2:
            raise SystemExit("could not retrieve at least two periods for a timeseries")
        out = {"question": question, "shape": shape, "attribute": attr, "series": series,
               "source": hit0["title"]}
        first, last = got[0], got[-1]
        out["change"] = round(last["value"] - first["value"], 2)
        if first["value"]:
            out["change_pct"] = round((last["value"] - first["value"]) / abs(first["value"]) * 100, 1)
        return out

    if shape == "comparison":
        items = [e for e in (ctx.get("entities") or []) if e][:8]
        subs = [(e, f"{attr} for {e}") for e in items]
    else:
        yrs = [y for y in (ctx.get("periods") or []) if y][:20]
        ent = ctx.get("entity") or ""
        subs = [(y, f"{attr} for {ent} in {y}") for y in yrs]
    if len(subs) < 2:
        raise SystemExit(f"{shape} needs at least two {'entities' if shape == 'comparison' else 'periods'}")
    _say("status", icon="🧮", msg=f"Plan: {len(subs)} separate lookups in parallel, then compare "
                                 f"({', '.join(str(s[0]) for s in subs)})")

    def one_sub(item):
        label, sub = item
        try:
            r = retrieve_for(sub)
            return {"label": str(label), "value": r.get("value"), "source": r.get("source")}
        except SystemExit as e:
            return {"label": str(label), "value": None, "error": str(e)}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(6, len(subs))) as ex:
        series = list(ex.map(_with_emitter(one_sub), subs))    # preserves order
    for s in series:
        _say("status", icon="✅" if s.get("value") is not None else "↩️",
             msg=f"{s['label']}: {s.get('value') if s.get('value') is not None else 'no data'}")
    got = [s for s in series if isinstance(s.get("value"), (int, float))]
    if len(got) < 2:
        raise SystemExit(f"could not retrieve comparable values for {shape}")
    out = {"question": question, "shape": shape, "attribute": attr, "series": series,
           "source": got[0].get("source")}
    if shape == "comparison":
        best = max(got, key=lambda s: s["value"])
        out["highest"] = best["label"]
        out["difference"] = round(best["value"] - min(s["value"] for s in got), 2)
    else:
        first, last = got[0], got[-1]
        out["change"] = round(last["value"] - first["value"], 2)
        if first["value"]:
            out["change_pct"] = round((last["value"] - first["value"]) / abs(first["value"]) * 100, 1)
    return out


def _search(question, ctx=None, hits=None):
    """ONE backtracking search over every choice point: field -> entity -> key/granularity -> period,
    accepted only when the retrieved record actually answers the question (unit/currency/place check).
    `ctx`/`hits` may be passed in when the planner already ran discovery, to avoid repeating it."""
    if ctx is None or hits is None:
        ctx, hits = discover(question)
    if not hits:
        raise SystemExit("agent finder returned no sources")
    p = ctx.get("period") or "latest"
    intent = QueryIntent.from_context(question, ctx)
    trace = []
    steps = [
        ("hit", lambda s: hits),
        ("entity", lambda s: _entity_options(ctx.get("entity"), ctx.get("type"))),
        ("key", lambda s: _key_options(s, ctx)),
        ("period", lambda s: ([p, "latest"] if p != "latest" else ["latest"])),
    ]

    attempts = [0]
    tried_tables = set()                    # distinct tables actually reached, for the failure message
    done = {}                               # complete fetch identity -> outcome, within this question
    MAX_ATTEMPTS = 40                       # 3 entities x 2 periods x a couple keys is the honest ceiling;
                                            # beyond it the search is looping, not exploring — stop cleanly.

    def goal(s):
        if attempts[0] >= MAX_ATTEMPTS:
            raise SystemExit(
                f"no source could answer this. {len(tried_tables)} of {len(hits)} candidate tables "
                f"were tried in {attempts[0]} attempts before the search budget ran out"
                + ("" if len(tried_tables) >= len(hits) else
                   " — the remaining candidates were never reached, so this is not proof the data is absent")
                + ".")
        ent = (s.get("entity") or {}).get("label")
        # Identity of the actual request, not its display name: the same table can be fetched
        # for several entities, keys and periods, and only the whole tuple distinguishes them.
        identity = (s["hit"]["identifier"], (s.get("entity") or {}).get("qid"), ent,
                    json.dumps(s.get("key"), sort_keys=True, default=str), s.get("period"))
        if identity in done:
            raise Backtrack(f"already attempted ({done[identity]})")
        attempts[0] += 1
        tried_tables.add(s["hit"]["identifier"])
        _say("status", icon="📥", msg=f"Fetching live from “{s['hit']['title']}”"
             + (f" for {ent}…" if ent else "…"))
        attempt = Attempt(source=s["hit"].get("publisher") or s["hit"]["title"],
                          identifier=s["hit"]["identifier"], entity=s.get("entity"),
                          period=s.get("period") or "latest")
        connector = connectors.for_hit(s["hit"])
        _say("status", icon="🔎", msg="Checking the result actually answers your question…")
        try:
            evidence = connector.execute(
                intent, attempt, s["hit"], lambda: _fetch(s, ctx),
                adjudicator=lambda data, verdict: _answers(question, data, verdict))
        except connectors.Rejected as e:
            # The adjudicator judged the TABLE, not this particular entity/key/period, so
            # every remaining combination beneath it would be rejected for the same reason.
            done[identity] = "wrong table"
            trace.append(e.attempt)
            _say("status", icon="↩️", msg=f"Wrong table ({e}) — skipping it and trying the next candidate…")
            raise Prune("hit", f"answer rejected: {e}")
        except Backtrack as e:
            # A usable table with nothing for this key or period: the next key or period is
            # genuinely worth trying, so this stays an ordinary leaf failure.
            done[identity] = str(e)
            trace.append(attempt)
            _say("status", icon="↩️", msg=f"No usable data ({e}) — backtracking…")
            raise
        trace.append(attempt)
        _say("status", icon="✅", msg="Result checks out — composing the grounded answer…")
        return {**s, "_data": evidence.payload, "_evidence": evidence, "_attempts": trace}

    try:
        state = _solve(steps, goal, {})
    except Backtrack as e:
        raise SystemExit(f"no source could answer: {e}")
    return ctx, hits, state["hit"], hits.index(state["hit"]) + 1, state["_data"], state


def retrieve_for(question):
    """Discover + retrieve for one sub-question, ANY domain (no synthesis). Universal join
    primitive; returns a NORMALIZED numeric `value`. Backtracks across every choice point."""
    _ctx, _hits, hit, _tried, data, state = _search(question)
    # list-returning sources (NSF/NIH/USAspending awards) carry their number as the engine-computed
    # total, not a scalar `value` — without this a comparison over those sources finds nothing to compare
    val = data.get("value", data.get("value_usd", data.get("total_usd")))
    try:
        val = float(val)
    except (TypeError, ValueError):
        pass
    return {"source": hit["title"], "source_identifier": hit.get("identifier"),
            "value": val, "data": data,
            "attempts": [a.to_dict() for a in (state.get("_attempts") or [])]}


_CONCEPT_LEAF = None


def _spent():
    """This thread's usage so far — a refused or failed question still burned calls, and hiding
    that would make the cheap failures look free."""
    led = llm.ledger()
    return led.snapshot() if led is not None else None


def _spent_discovery():
    u = ard_client.usage()
    return u.snapshot() if u is not None else None


def _leaf_for_concept(concept):
    """The SEC leaf that pins a given us-gaap concept, keyed off the built index metadata."""
    global _CONCEPT_LEAF
    if _CONCEPT_LEAF is None:
        from registry import index as _ix
        _CONCEPT_LEAF = {}
        try:
            for m in json.load(open(_ix.CACHE_META)):
                if m.get("concept"):
                    _CONCEPT_LEAF.setdefault(m["concept"], m)
        except Exception:
            pass
    return _CONCEPT_LEAF.get(concept)


def _cite_concept_actually_used(hit, data):
    """Cite the concept that ANSWERED, not the one discovery ranked.

    driver.fetch_metric re-discovers the us-gaap concept from the attribute and returns the first
    one the company actually reports, so a leaf that 404s (AssetsNet for a carmaker) can be the
    ranked hit while the number comes from another concept (Assets). Reporting the ranked leaf then
    labels a correct figure with the wrong table. The grant-graph branch already re-cites for the
    same reason; this does it for SEC."""
    used = (data or {}).get("concept") or ""
    if not used.startswith("us-gaap:"):
        return hit
    leaf = _leaf_for_concept(used.split(":", 1)[1])
    if not leaf or leaf["identifier"] == hit.get("identifier"):
        return hit
    return {"identifier": leaf["identifier"], "title": leaf.get("title", hit.get("title", "")),
            "publisher": hit.get("publisher") or "sec-edgar"}


def _admit(intent, hit, data):
    """Normalize a non-backtracking execution path through the same connector/validation boundary."""
    attempt = Attempt(source=hit.get("publisher") or hit.get("title") or "",
                      identifier=hit.get("identifier") or "", period=intent.period)
    evidence = connectors.for_hit(hit).execute(
        intent, attempt, hit, lambda: data,
        adjudicator=lambda payload, verdict: _answers(intent.question, payload, verdict))
    return evidence, [attempt]


def _present(question, evidence):
    """Validated evidence chooses the deterministic renderer. Complex evidence explicitly falls
    back to synthesis; classifier shape never selects authoritative prose."""
    rendered = renderers.render(evidence)
    if rendered:
        return rendered.text, rendered.renderer
    return TK.synthesize(question, evidence.payload), "llm-fallback"


def run(question, sites=None, assumptions=None, on_ambiguity="answer"):
    # Account for the LLM calls this question makes IN THIS PROCESS. The ARD Agent Finder runs as
    # a separate service and bills its own discovery work — reported alongside as `discovery_usage`,
    # deliberately a SIBLING of `usage` rather than nested in it, so nothing reads as part of the
    # question's own total.
    _ledger = llm.start_ledger()
    _disc = ard_client.start_usage()
    if on_ambiguity not in ("answer", "ask", "all"):
        on_ambiguity = "answer"
    # PLAN BEFORE FETCH. The shape of the question and the DECLARED capability of the candidate
    # sources decide whether this is one call, several, or impossible — and an impossible question
    # is refused here, without issuing a single request.
    ctx, hits = discover(question, sites, assumptions)
    if not hits:
        raise SystemExit("agent finder returned no sources")
    shape = ctx.get("shape") if ctx.get("shape") in planner.SHAPES else "point"
    intent = QueryIntent.from_context(question, ctx, sites)
    # A genuinely ambiguous measure over a single entity gets SEPARATE answers per interpretation
    # (earnings -> net income, EBITDA, EPS…) instead of a silently-chosen one.
    if len(ctx.get("interpretations") or []) >= 2 and shape in ("point", "status", "entity-list"):
        data = _run_ambiguous(question, ctx)
        clarification = _clarification(ctx.get("attribute") or "the requested measure",
                                       ctx.get("entity") or "", data.get("interpretations") or [])
        if clarification:
            trace = [attempt for option in (data.get("interpretations") or [])
                     for attempt in (option.get("attempts") or [])]
            return _ambiguity_result(question, ctx, hits, intent, clarification, on_ambiguity,
                                     _ledger, _disc, trace)
        # Distinct taxonomy labels that returned the same value are aliases, not a useful question
        # for a human. Preserve the old combined answer rather than interrupting the caller.
        evidence, attempts = _admit(intent, hits[0], data)
        _answer, renderer = _present(question, evidence)
        return {"question": question, "answer": _answer, "shape": shape, "usage": _ledger.snapshot(),
                "discovery_usage": _disc.snapshot(),
                "intent": intent.to_dict(), "attempts": [a.to_dict() for a in attempts],
                "evidence": evidence.to_dict(), "answer_renderer": renderer,
                "plan": f"ambiguous measure → {len(data['interpretations'])} interpretations answered separately",
                "source": {"identifier": hits[0]["identifier"], "title": hits[0]["title"],
                           "publisher": hits[0].get("publisher")},
                "candidates": [{"identifier": h.get("identifier"), "title": h["title"],
                                "score": h["score"], "publisher": h.get("publisher")}
                               for h in hits],
                "data": data}
    p = planner.plan(shape, hits, ctx.get("quantifier") or "exhaustive")
    _say("plan_chosen", shape=shape, verdict=p["verdict"], why=p.get("why", ""),
         summary=planner.describe(shape, p))

    # Grant-graph leaves (IRS 990 who-funds-whom) traverse a local edge table in a direction their
    # marker declares, so they route before the generic shape gating rather than through it. The
    # classifier already gated whether the philanthropic grant graph is even in the pool; once a grant
    # leaf is among the candidates, prefer it over keyword-adjacent siblings (federal awards, a
    # nonprofit's own contributions) — so scan all hits, not just the top, and never wrongly refuse it.
    # Only when a grant leaf is the planner's pick OR near the TOP of discovery — otherwise a stray
    # low-ranked grant leaf would hijack a non-grant question (e.g. "what does Feeding America do").
    grant_hit = next((h for h in ([p["hit"]] if p.get("hit") else []) + hits[:2]
                      if (driver.frontmatter(h["identifier"]) or {}).get("irsgrants")), None)
    if grant_hit:
        data = _run_grants(question, ctx, {"hit": grant_hit})
        # Cite the leaf matching the traversal actually run, not just whichever grant leaf discovery
        # surfaced first (the direction is picked in code, so the two can differ).
        _GRANT_LEAF = {"forward": "grants-made", "reverse": "grants-received", "ranking": "top-grantmakers",
                       "biggest_recipients": "biggest-recipients", "geo": "geographic",
                       "overview": "grant-overview", "shared": "shared-grantees", "theme": "grants-by-cause"}
        import grants as _g
        leaf_id = f"sources/irs-grants/{_GRANT_LEAF.get(_grant_direction(question, ctx, _g), 'grants-made')}.md"
        hit = next((h for h in hits if h["identifier"] == leaf_id), None)
        if not hit:
            fm = driver.frontmatter(leaf_id) or {}
            hit = {"identifier": leaf_id, "title": fm.get("title", grant_hit["title"]),
                   "publisher": grant_hit.get("publisher")}
        evidence, attempts = _admit(intent, hit, data)
        _answer, renderer = _present(question, evidence)
        return {"question": question, "answer": _answer, "shape": shape, "usage": _ledger.snapshot(),
                "discovery_usage": _disc.snapshot(),
                "intent": intent.to_dict(), "attempts": [a.to_dict() for a in attempts],
                "evidence": evidence.to_dict(), "answer_renderer": renderer,
                "plan": planner.describe(shape, p),
                "source": {"identifier": hit["identifier"], "title": hit["title"], "publisher": hit.get("publisher")},
                "candidates": [{"identifier": h.get("identifier"), "title": h["title"],
                                "score": h["score"], "publisher": h.get("publisher")} for h in hits],
                "data": data}

    if p["verdict"] == "infeasible":
        need = ("a source that can see a whole population" if shape in ("ranking", "aggregate", "filtered-subset")
                else "a capability none of the matching sources declare")
        raise SystemExit(f"this is a '{shape}' question, which needs {need}; {p['why']}.")

    state = None
    if p["verdict"] == "compose:materialize-and-correlate":
        data = _run_correlate(question, ctx, p)
        hit = p["hit"]
    elif p["verdict"] == "compose:derive":
        data = _run_derive(question, ctx, p)
        hit = p["hit"]
    elif p["verdict"] == "compose:generate-and-test":
        data = _run_generate_test(question, ctx, p)
        hit = p["hit"]
    elif p["verdict"].startswith("compose:fan-out"):
        data = _run_fanout(question, ctx, shape)
        hit = p["hit"]
    elif p["verdict"].startswith("compose:scan-and") or shape in ("ranking", "aggregate", "filtered-subset"):
        # a BigQuery-backed population leaf runs SQL, not the REST accessor
        if (driver.frontmatter(p["hit"]["identifier"]) or {}).get("bq"):
            data = _run_bq(question, ctx, p)
        else:
            data = _run_ranking(question, ctx, p)
        hit = p["hit"]
    else:
        _ctx, hits, hit, _tried, data, _state = _search(question, ctx=ctx, hits=hits)
        state = _state

    # SEC concept resolution already fetches a pool of reported sibling measures. If more than one
    # remains plausible and their returned values materially differ, expose that empirical ambiguity
    # here. `ask` and `all` become terminal outcomes; `answer` continues with the selected value but
    # carries every alternative in the response.
    resolution = data.pop("_ambiguity", None) if isinstance(data, dict) else None
    if resolution:
        clarification = _clarification(resolution.get("attribute") or intent.measure,
                                       intent.entity or "", resolution.get("options") or [])
        if clarification and on_ambiguity in ("ask", "all"):
            ordered_hits = [hit] + [h for h in hits if h.get("identifier") != hit.get("identifier")]
            trace = [a.to_dict() for a in ((state or {}).get("_attempts") or [])]
            return _ambiguity_result(question, ctx, ordered_hits, intent, clarification, on_ambiguity,
                                     _ledger, _disc, trace)
        if clarification:
            data["ambiguity"] = {"attribute": clarification.attribute,
                                 "reason": resolution.get("reason"),
                                 "options": clarification.to_dict()["options"]}
    hit = _cite_concept_actually_used(hit, data)
    if state and state.get("_evidence"):
        evidence, attempts = state["_evidence"], state.get("_attempts") or []
        evidence.identifier = hit["identifier"]
        evidence.provenance["source_document"] = hit["identifier"]
        if data.get("ambiguity"):
            evidence.warnings.append("other materially different interpretations are included in data.ambiguity")
    else:
        evidence, attempts = _admit(intent, hit, data)
    answer, renderer = _present(question, evidence)
    return {
        "question": question,
        "answer": answer,
        "usage": _ledger.snapshot(),
        "discovery_usage": _disc.snapshot(),
        "shape": shape,
        "intent": intent.to_dict(),
        "attempts": [a.to_dict() for a in attempts],
        "evidence": evidence.to_dict(),
        "answer_renderer": renderer,
        "plan": planner.describe(shape, p),
        "source": {"identifier": hit["identifier"], "title": hit["title"], "publisher": hit.get("publisher")},
        "candidates": [{"identifier": h.get("identifier"), "title": h["title"],
                        "score": h["score"], "publisher": h.get("publisher")} for h in hits],
        "data": data,
    }


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Data Query</title>
<style>
 body{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#1a1a1a}
 h1{font-size:1.5rem;margin-bottom:.2em} .sub{color:#666;margin-top:0}
 form{display:flex;gap:8px;margin:18px 0} input{flex:1;padding:11px 13px;font-size:1rem;border:1px solid #ccc;border-radius:8px}
 button{padding:11px 18px;font-size:1rem;border:0;border-radius:8px;background:#1a73e8;color:#fff;cursor:pointer}
 button:disabled{background:#9bb7ea}
 .ex{display:inline-block;margin:3px 6px 3px 0;padding:5px 10px;background:#eef2f7;border-radius:14px;font-size:.85rem;cursor:pointer;color:#334}
 #out{margin-top:22px} .answer{font-size:1.15rem;padding:16px 18px;background:#f6f9f6;border-left:4px solid #34a853;border-radius:6px}
 .src{margin-top:10px;color:#444} .pub{color:#888} .err{padding:14px;background:#fdecea;border-left:4px solid #d93025;border-radius:6px}
 .loading{color:#888} details{margin-top:12px;color:#555} summary{cursor:pointer} li{font-size:.9rem;color:#555}
 .sh{font-size:1.1rem;margin:34px 0 4px;padding-top:20px;border-top:1px solid #eee} .shsub{color:#888;margin:0 0 14px;font-size:.9rem}
 .src-card{padding:13px 15px;margin:10px 0;border:1px solid #e6e6e6;border-radius:10px;background:#fafbfc}
 .src-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
 .src-name{font-weight:600;color:#1a3050} .cnt{color:#888;font-size:.8rem;white-space:nowrap}
 .covers{color:#666;font-size:.88rem;margin:3px 0 8px} .chips{display:flex;flex-wrap:wrap}
 .cats{display:flex;flex-wrap:wrap;gap:5px;margin-top:2px}
 .cat{font-size:.74rem;color:#41506a;background:#eef2f7;border:1px solid #e0e6ee;border-radius:11px;padding:2px 8px}
 .extag{display:block;font-size:.68rem;color:#7a8899;margin-top:2px;font-variant:all-small-caps;letter-spacing:.03em}
 .ex.exr{background:#fdecea;color:#6b2b23} .ex.exr .extag{color:#a5564a}
 .tabbar{display:flex;flex-wrap:wrap;gap:7px;margin:6px 0 14px}
 .tab{padding:7px 13px;border:1px solid #d5dbe2;border-radius:18px;background:#fff;cursor:pointer;font-size:.9rem;color:#33455c;user-select:none}
 .tab:hover{border-color:#9bb7ea} .tab.on{background:#1a73e8;color:#fff;border-color:#1a73e8}
 #panel{min-height:40px}
 .log{margin-top:20px;padding:16px 18px;background:#0d1117;border-radius:10px;font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;color:#c9d1d9;max-height:60vh;overflow-y:auto}
 .ln{opacity:0;transform:translateY(4px);animation:in .32s ease forwards;margin:2px 0;display:flex;gap:9px;align-items:flex-start}
 @keyframes in{to{opacity:1;transform:none}}
 .ic{flex:0 0 auto;width:1.4em;text-align:center} .txt{flex:1}
 .ardlink{display:inline-block;margin-top:7px;font-size:.83em;color:#1a73e8;text-decoration:none}
 .ardlink:hover{text-decoration:underline}
 .cost{color:#8b949e;margin-top:6px;font-size:.86em}
 table.costs{border-collapse:collapse;margin:8px 0;font-size:.86em;color:#8b949e;width:100%;max-width:460px}
 table.costs th,table.costs td{padding:3px 10px 3px 0;text-align:left;border-bottom:1px solid #21262d}
 table.costs th{color:#6e7681;font-weight:500}
 table.costs td.n,table.costs th.n{text-align:right;font-variant-numeric:tabular-nums}
 table.costs tr.sep td{border-top:1px solid #30363d}
 table.costs tr.tot td{color:#c9d1d9;font-weight:600;border-bottom:none}
 .plan{color:#e3b341} .plan b{color:#f0f6fc;font-weight:600} .scan{color:#8b949e;margin-top:3px;font-size:.9em}
 .cand{color:#8b949e;margin:1px 0 1px 2.3em;display:flex;align-items:center;gap:8px;font-size:.92em}
 .bar{height:7px;border-radius:4px;background:#58a6ff;min-width:4px} .ct{color:#c9d1d9;flex:1}
 .cs{color:#6e7681;width:2.2em;text-align:right} .win{color:#3fb950!important;font-weight:600}
 .rslv{color:#79c0ff} .rslv b{color:#f0f6fc} .keyk{color:#6e7681} .back{color:#f85149}
 .cur{display:inline-block;width:.6em;color:#58a6ff;animation:blink 1s step-start infinite}
 @keyframes blink{50%{opacity:0}}
 .shape{color:#d2a8ff} .shape b{color:#f0f6fc}
 .rank{margin-top:14px} .rank table{border-collapse:collapse;width:100%;font-size:.9rem}
 .rank td{padding:5px 8px;border-bottom:1px solid #eee} .rank tr:first-child td{font-weight:600;color:#137333}
 .rank .n{color:#999;width:2em} .rank .v{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
 .scope{margin-top:14px;padding:11px 14px;background:#fff8e6;border-left:4px solid #e0a800;border-radius:6px;font-size:.9rem;color:#5a4a1a}
 .scope b{color:#3d3210} .scope ul{margin:7px 0 0;padding-left:18px} .scope li{font-size:.86rem;color:#5a4a1a}
 .clarify{padding:16px 18px;background:#fff8e6;border-left:4px solid #e0a800;border-radius:6px}
 .clarify p{margin:0 0 10px}.clarify-choice{display:block;width:100%;margin:7px 0;padding:10px 12px;text-align:left;background:#fff;color:#27364a;border:1px solid #d7c47a}
 .clarify-choice:hover{background:#fffdf5}.choice-value{float:right;color:#137333;font-weight:600}
 .recs{margin-top:14px} .recs-h{font-size:.85rem;color:#888;margin:0 0 6px}
 .rec{padding:11px 14px;margin:8px 0;border:1px solid #e6e6e6;border-radius:10px;background:#fafbfc}
 .rec-t{font-weight:600;color:#1a3050;margin-bottom:5px}
 .rec-f{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:.85rem;color:#555} .rec-f b{color:#222;font-weight:600}
 .amt{color:#137333;font-weight:700}
</style></head><body>
<h1>Agentic Data Query</h1>
<p class="sub">Ask a question in plain English. An ARD Agent Finder discovers which dataset answers it; the data is fetched live, the answer is checked, and the search backtracks until it actually answers your question. <a href="how-it-works" style="color:#1a73e8">How it works ›</a> · <a href="life-of-a-query" style="color:#1a73e8">Life of a query ›</a> · <a href="techsoup" style="color:#1a73e8">TechSoup view ›</a></p>
<form id="f"><input id="q" placeholder="e.g. Is the American Red Cross a 501(c)(3)?" autofocus><button id="b">Ask</button></form>
<div id="out"></div>
<h2 class="sh">Example questions</h2>
<p class="shsub">Pick a theme, then click a question to run it live.</p>
<div id="tabbar" class="tabbar"></div>
<div id="panel" class="chips"></div>
<h2 class="sh">Data sources</h2>
<p class="shsub">The sources behind the selected theme — each described once as an OKF document; discovery and access are generic.</p>
<div id="sources"></div>
<script>
 var f=document.getElementById('f'),q=document.getElementById('q'),b=document.getElementById('b'),out=document.getElementById('out');
 var ASSUMPTIONS=null;
 function bindChips(){[].forEach.call(document.querySelectorAll('.ex'),function(el){el.onclick=function(){
   var tag=el.querySelector('.extag'); var txt=el.textContent;
   if(tag)txt=txt.slice(0,txt.length-tag.textContent.length);      // strip the shape badge
   q.value=txt.trim();window.scrollTo(0,0);f.requestSubmit();}});}
 var TABS=[],SRCS=[];
 function renderSources(dirs){
   var list=(dirs&&dirs.length)?SRCS.filter(function(s){return dirs.indexOf(s.dir)>=0}):SRCS;
   document.getElementById('sources').innerHTML=list.map(function(s){
     var cats=(s.categories||[]).map(function(c){return '<span class="cat">'+esc(c)+'</span>'}).join('');
     return '<div class="src-card"><div class="src-head"><span class="src-name">'+esc(s.name)+'</span>'
       +'<span class="cnt">'+s.count+(s.count==1?' endpoint':' tables')+'</span></div>'
       +'<div class="covers">'+esc(s.covers)+'</div>'+(cats?'<div class="cats">'+cats+'</div>':'')
       +'<a class="ardlink" href="ard?source='+encodeURIComponent(s.dir)+'">browse '+s.count
       +' ARD '+(s.count==1?'entry':'entries')+' \u2192</a></div>';
   }).join('');}
 function showPanel(i){var t=TABS[i]||{queries:[]};
   document.getElementById('panel').innerHTML=(t.queries||[]).map(function(qy){
     if(typeof qy==='string')return '<span class="ex">'+esc(qy)+'</span>';
     var cls=/refused/.test(qy.tag||'')?' exr':'';
     return '<span class="ex'+cls+'">'+esc(qy.q)+'<span class="extag">'+esc(qy.tag||'')+'</span></span>';
   }).join('');
   bindChips();renderSources(t.dirs);}
 function renderTabs(tabs){TABS=tabs||[];var bar=document.getElementById('tabbar');
   bar.innerHTML=TABS.map(function(t,i){return '<span class="tab'+(i===0?' on':'')+'" data-i="'+i+'">'+esc(t.label)+'</span>'}).join('');
   [].forEach.call(bar.querySelectorAll('.tab'),function(el){el.onclick=function(){
     [].forEach.call(bar.querySelectorAll('.tab'),function(x){x.classList.remove('on')});
     el.classList.add('on');showPanel(+el.getAttribute('data-i'));};});
   showPanel(0);}
 fetch('sources').then(function(r){return r.json()}).then(function(d){
   SRCS=d.sources||[];renderTabs(d.tabs);
 });
 f.onsubmit=function(e){e.preventDefault();var question=q.value.trim();if(!question)return;
   b.disabled=true;
   out.innerHTML='<div class="log" id="log"></div>';
   var log=document.getElementById('log');
   var cursor=document.createElement('div');cursor.className='ln';cursor.innerHTML='<span class="cur">▋</span>';log.appendChild(cursor);
   function push(html){var d=document.createElement('div');d.className='ln';d.innerHTML=html;log.insertBefore(d,cursor);log.scrollTop=log.scrollHeight;return d;}
   function status(icon,txt,cls){return push('<span class="ic">'+icon+'</span><span class="txt '+(cls||'')+'">'+txt+'</span>');}
   var stalled=false,wd=null;
   function fin(){if(cursor)cursor.parentNode&&cursor.remove();b.disabled=false;if(wd)clearTimeout(wd);}
   // Watchdog: a stream that stops arriving mid-flight would otherwise leave the spinner up forever
   // (the reader loop only ends on a clean close). Say so instead of hanging silently.
   function beat(){if(wd)clearTimeout(wd);wd=setTimeout(function(){stalled=true;
     status('⚠️','The server stopped sending updates. It may still be working — check the terminal, or ask again.','back');fin();},120000);}
   beat();
   var askUrl='ask?sse_format=named&max_results=8&on_ambiguity=ask&query='+encodeURIComponent(question);
   if(ASSUMPTIONS){Object.keys(ASSUMPTIONS).forEach(function(k){
     askUrl+='&assumption_'+k+'='+encodeURIComponent(ASSUMPTIONS[k]||'');});ASSUMPTIONS=null;}
   fetch(askUrl)
    .then(function(resp){
      var reader=resp.body.getReader(),dec=new TextDecoder(),buf='';
      function pump(){return reader.read().then(function(res){
        if(stalled)return;
        if(res.done){fin();return;}
        beat();
        buf+=dec.decode(res.value,{stream:true});
        var parts=buf.split('\n\n');buf=parts.pop();
        // An SSE frame may carry event:/id: lines before data:, so pull the data line out rather
        // than assuming the frame starts with it — with sse_format=named it never does.
        parts.forEach(function(p){
          var payload=null;
          p.split('\n').forEach(function(ln){if(ln.indexOf('data:')===0)payload=ln.slice(5).trim();});
          if(!payload)return;
          var ev;try{ev=JSON.parse(payload)}catch(_){return;}
          handle(ev);});
        return pump();
      });}
      return pump();
    }).catch(function(err){if(!stalled){status('⚠️',esc(String(err)),'back');fin();}});
   function handle(ev){
     // NLWeb message stream: lifecycle, narration, results, then the generated answer.
     var t=ev.message_type, c=ev.content;
     if(t==='intermediate_message'){
       // the engine prefixes its narration with an emoji; split it back out so the icon column
       // lines up the way it always has
       var s0=String(c||'').trim(), m=s0.match(/^(\p{Extended_Pictographic}\uFE0F?)\s*([\s\S]*)$/u);
       status(m?m[1]:'\u2022', esc(m?m[2]:s0), s0.indexOf('backtrack')>=0?'back':'');}
     else if(t==='result'){renderItems(c||[]);}
     else if(t==='nlws'){renderAnswer(c||{});}
     else if(t==='error'){status('⚠️',esc(String(c||'No answer.')),'back');}
     else if(t==='end-nlweb-response'){fin();}
   }
   function renderItems(items){
     status('\u{1F4DA}','ARD returned '+items.length+' candidate table'+(items.length==1?'':'s')+':');
     var mx=Math.max.apply(null,items.map(function(c){return c.score||0}).concat([1]));
     items.forEach(function(c,i){var w=Math.round(6+((c.score||0)/mx)*120);
       var d=document.createElement('div');d.className='cand';
       d.innerHTML='<span class="cs">'+(c.score||0)+'</span><span class="bar'+(i===0?' win':'')
         +'" style="width:'+w+'px"></span><span class="ct'+(i===0?' win':'')+'">'+esc(c.name)
         +'</span> <span class="pub">'+esc(c.site||'')+' · '+esc(c.tier||'')+'</span>';
       log.insertBefore(d,cursor);});
     log.scrollTop=log.scrollHeight;
   }
   function renderAnswer(d){
     if(d['@type']==='ClarificationRequest'){
       var opts=d.options||[], h='<div class="clarify"><p><b>'+esc(d.question||'Which interpretation do you mean?')+'</b></p>';
       opts.forEach(function(o){
         var val=(String(o.unit||'').toUpperCase()==='USD'?money(o.value):String(o.value)+(o.unit?' '+o.unit:''));
         h+='<button type="button" class="clarify-choice" data-assumptions="'
           +encodeURIComponent(JSON.stringify(o.assumptions||{}))+'">'+esc(o.label||o.id)
           +'<span class="choice-value">'+esc(val||o.value)+'</span></button>';});
       h+='</div>';if(d.usage)h+=renderUsage(d.usage,d.discovery_usage);
       var box=document.createElement('div');box.style.marginTop='16px';box.innerHTML=h;log.parentNode.appendChild(box);
       [].forEach.call(box.querySelectorAll('.clarify-choice'),function(choice){choice.onclick=function(){
         ASSUMPTIONS=JSON.parse(decodeURIComponent(choice.getAttribute('data-assumptions')));f.requestSubmit();};});
       return;
     }
     if(!d.answer){status('⚠️','No answer.','back');return;}
     var h='<div class="answer">'+esc(d.answer)+'</div>';
     if(d.data&&d.data.ambiguous&&Array.isArray(d.data.interpretations))h+=renderInterp(d.data.interpretations);
     if(d.data&&d.data.match==='name'&&d.data.matched_entities>1)h+=renderScope(d.data);
     if(d.data&&Array.isArray(d.data.ranking)&&d.data.ranking.length)h+=renderRank(d.data.ranking,'');
     if(d.data&&Array.isArray(d.data.series)&&d.data.series.length)h+=renderRank(
        d.data.series.filter(function(s){return s.value!=null}).map(function(s){
          return {label:s.label,value:s.value}}),' ');
     if(d.data&&Array.isArray(d.data.results)&&d.data.results.length)h+=renderRecords(d.data.results);
     var it=(d.items||[])[0];
     if(it)h+='<div class="src">\u{1F4DA} <a href="'+esc(it.url)+'">'+esc(it.name)
       +'</a> <span class="pub">['+esc(it.site||'')+']</span></div>';
     if((d.items||[]).length>1){h+='<details><summary>ARD candidates</summary><ul>';
       d.items.forEach(function(c){h+='<li>'+(c.score||0)+' — '+esc(c.name)+'</li>'});h+='</ul></details>';}
     if(d.intent||d.evidence||(d.attempts||[]).length){
       h+='<details><summary>How this answer was produced</summary>';
       if(d.intent)h+='<p><b>Interpretation:</b> '+esc(d.intent.operation||'')+' · '
          +esc(d.intent.entity||'no named entity')+' · '+esc(d.intent.measure||'')+' · '
          +esc(d.intent.period||'latest')+' <button type="button" class="edit-intent" data-intent="'
          +encodeURIComponent(JSON.stringify(d.intent))+'">edit & rerun</button></p>';
       if(d.attempts&&d.attempts.length){h+='<ol>';
         d.attempts.forEach(function(a){h+='<li><code>'+esc(a.identifier||a.source||'candidate')+'</code> — '
           +esc(a.outcome||'')+(a.reason?' · '+esc(a.reason):'');
           if(a.validation&&a.validation.checks)h+='<ul>'+a.validation.checks.map(function(c){return '<li>'
             +esc(c.name)+': '+esc(c.status)+(c.reason?' — '+esc(c.reason):'')+'</li>';}).join('')+'</ul>';
           h+='</li>';});h+='</ol>';}
       if(d.evidence)h+='<p><b>Evidence:</b> '+esc(d.evidence.kind||'')+' from '
          +'<code>'+esc(d.evidence.identifier||'')+'</code> · renderer '+esc(d.answer_renderer||'')+'</p>';
       h+='</details>';}
     if(d.usage)h+=renderUsage(d.usage,d.discovery_usage);
     var box=document.createElement('div');box.style.marginTop='16px';box.innerHTML=h;log.parentNode.appendChild(box);
     var edit=box.querySelector('.edit-intent');if(edit)edit.onclick=function(){
       var x=JSON.parse(decodeURIComponent(edit.getAttribute('data-intent')));
       ASSUMPTIONS={operation:prompt('Operation',x.operation||'point')||x.operation,
                    entity:prompt('Entity',x.entity||'')||'',
                    type:prompt('Entity type',x.entity_type||'none')||x.entity_type,
                    measure:prompt('Measure',x.measure||'')||x.measure,
                    period:prompt('Period',x.period||'latest')||x.period};
       f.requestSubmit();};
   }
   function usd(c){return c>=0.01?'$'+c.toFixed(3):(c>0?'$'+c.toFixed(5):'$0');}
   // Steps in PIPELINE order, not sorted by cost — the point of the report is to show where a
   // question's spend goes as it moves through the engine, and ordering by size hides that shape.
   var STEP_ORDER = ['classify','resolve-entity','resolve-concept','check','synthesize','other'];
   var STEP_LABEL = {
     'classify':'classify the question', 'resolve-entity':'resolve the entity',
     'resolve-concept':'resolve the measure', 'check':'check the answer fits',
     'synthesize':'write the answer', 'other':'other'};
   function renderUsage(u,dz){
     var h='<div class="cost">\u26A1 this query: '+u.llm_calls+' LLM calls ('+u.chat_calls+' chat, '
         + u.embed_calls+' embed) \u00B7 '+Number(u.total_tokens).toLocaleString()+' tokens \u00B7 '
         + usd(u.cost_usd)+'<span class="pub"> ['+(u.cost_source==='provider'?'billed':'estimated')
         + ']</span></div>';
     if(dz&&dz.searches)h+='<div class="cost">\u{1F50E} agent finder (separate service, not counted '
         + 'above): '+dz.searches+' searches \u00B7 '+dz.llm_calls+' LLM calls \u00B7 '
         + Number(dz.total_tokens).toLocaleString()+' tokens \u00B7 '+usd(dz.cost_usd)+'</div>';

     var st=u.by_stage||{}, keys=Object.keys(st);
     STEP_ORDER.forEach(function(k){if(keys.indexOf(k)<0)keys.push(k)});
     var rows='', tot=0, toks=0;
     STEP_ORDER.forEach(function(k){
       var v=st[k]; if(!v) return;
       tot+=v.cost_usd; toks+=v.tokens;
       rows+='<tr><td>'+esc(STEP_LABEL[k]||k)+'</td><td class="n">'+v.calls+'</td><td class="n">'
           + Number(v.tokens).toLocaleString()+'</td><td class="n">'+usd(v.cost_usd)+'</td></tr>';
     });
     if(dz&&dz.llm_calls)
       rows+='<tr class="sep"><td>discovery <span class="pub">(agent finder)</span></td><td class="n">'
           + dz.llm_calls+'</td><td class="n">'+Number(dz.total_tokens).toLocaleString()
           + '</td><td class="n">'+usd(dz.cost_usd)+'</td></tr>';
     var grand=tot+((dz&&dz.cost_usd)||0), gtok=toks+((dz&&dz.total_tokens)||0);
     rows+='<tr class="tot"><td>total</td><td class="n">'+(u.llm_calls+((dz&&dz.llm_calls)||0))
         + '</td><td class="n">'+Number(gtok).toLocaleString()+'</td><td class="n">'+usd(grand)+'</td></tr>';
     if(rows)h+='<details><summary>Cost report \u2014 per step</summary>'
         + '<table class="costs"><thead><tr><th>step</th><th class="n">calls</th>'
         + '<th class="n">tokens</th><th class="n">cost</th></tr></thead><tbody>'+rows
         + '</tbody></table>'
         + '<p class="pub">prompt '+Number(u.prompt_tokens).toLocaleString()+' \u00B7 completion '
         + Number(u.completion_tokens).toLocaleString()+' \u00B7 embedding '
         + Number(u.embed_tokens).toLocaleString()
         + '. Resolution steps are cached per process, so a repeat question about the same entity '
         + 'skips them.</p></details>';
     return h;
   }
   function trunc(s,n){s=String(s);return s.length>n?s.slice(0,n-1)+'…':s;}
   function clean(v){if(v==null)return null;v=String(v).trim();return (v===''||v==='null'||v==='undefined')?null:v;}
   function firstStr(v){if(Array.isArray(v))v=v.length?v[0]:null;return clean(v);}
   function pick(o,keys){for(var i=0;i<keys.length;i++){var v=o[keys[i]];if(Array.isArray(v))v=v.length?v[0]:null;if(clean(v)!=null)return v;}return null;}
   function money(v){var n=Number(String(v).replace(/[^0-9.\-]/g,''));if(!isFinite(n)||!n)return null;
     return '$'+n.toLocaleString('en-US',{maximumFractionDigits:0});}
   function renderInterp(items){
     var rs=items.map(function(a){
       var v=a.value==null?'<span style="color:#999">unavailable</span>'
             :(Math.abs(a.value)>=1000?money(a.value):String(a.value))+(a.unit?' '+esc(a.unit):'');
       return '<tr><td>'+esc(a.interpretation||a.label||a.id)+'</td><td class="v">'+v+'</td></tr>';}).join('');
     return '<div class="rank"><p class="recs-h">the measure is ambiguous — one answer per interpretation</p>'
            +'<table>'+rs+'</table></div>';
   }
   function renderRank(rows,pfx){
     var rs=rows.slice(0,10).map(function(r,i){
       var v=(Math.abs(r.value)>=1000)?money(r.value):String(r.value);
       return '<tr><td class="n">'+(pfx?'':(i+1)+'.')+'</td><td>'+esc(trunc(r.label,54))+'</td>'
             +'<td class="v">'+esc(v||r.value)+'</td></tr>';}).join('');
     return '<div class="rank"><table>'+rs+'</table></div>';
   }
   function renderScope(dt){
     var gs=(dt.entity_groups||[]).slice(0,6).map(function(g){
       var amt=money(g.total_usd);
       return '<li>'+esc(trunc(g.name,58))+(amt?' — '+amt:'')+' <span style="color:#8a7a4a">('+g.count+')</span></li>';
     }).join('');
     var more=(dt.entity_groups||[]).length>6?'<li>…</li>':'';
     return '<div class="scope">⚠️ Matched by <b>name</b>, not a canonical identifier — these rows span <b>'
       +dt.matched_entities+'</b> separately registered recipients, so any total is across all of them.'
       +'<ul>'+gs+more+'</ul></div>';
   }
   function renderRecords(results){
     var rows=results.slice(0,8).map(function(o){
       var amt=money(pick(o,['fundsObligatedAmt','estimatedTotalAmt','Award Amount','awardCeiling','total_obligated','award_amount']));
       var pi=firstStr(pick(o,['pdPIName','Principal Investigator','pi']));
       var awd=firstStr(pick(o,['awardeeName','awardee','Recipient Name','recipient']));
       var date=firstStr(pick(o,['startDate','date','Start Date','postedDate']));
       var prog=firstStr(pick(o,['fundProgramName','program','agency','Awarding Agency']));
       // Not every source titles its records (USAspending awards have no title at all) — fall back to
       // the recipient, then to an award identifier, and don't repeat whatever became the heading.
       var title=firstStr(pick(o,['title','Award Title','opportunityTitle','name']));
       var titleIsAwardee=false;
       if(!title&&awd){title=awd;titleIsAwardee=true;}
       if(!title)title=firstStr(pick(o,['Award Type','generated_internal_id','Award ID','id','internal_id']))||'Award';
       var f=[];
       if(amt)f.push('<span class="amt">'+amt+'</span>');
       if(pi)f.push('<span><b>PI</b> '+esc(trunc(pi,34))+'</span>');
       if(awd&&!titleIsAwardee)f.push('<span><b>Awardee</b> '+esc(trunc(awd,42))+'</span>');
       if(date)f.push('<span><b>Date</b> '+esc(date)+'</span>');
       if(prog)f.push('<span><b>Program</b> '+esc(trunc(prog,42))+'</span>');
       return '<div class="rec"><div class="rec-t">'+esc(trunc(title,96))+'</div><div class="rec-f">'+f.join('')+'</div></div>';
     }).join('');
     return '<div class="recs"><p class="recs-h">'+results.length+' record'+(results.length==1?'':'s')+'</p>'+rows+'</div>';
   }
 };
 function esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]});}
</script></body></html>"""


# The TechSoup page reuses the main page's entire interaction (streaming console, live query,
# result rendering) — only the framing copy and the tab source differ, so it is derived by
# substitution rather than duplicated.
TECHSOUP_PAGE = (PAGE
    .replace("<title>Agentic Data Query</title>", "<title>Data for Nonprofits — a TechSoup view</title>")
    .replace('<h1>Agentic Data Query</h1>',
             '<h1>Data for Nonprofits</h1>')
    .replace('<p class="sub">Ask a question in plain English. An ARD Agent Finder discovers which '
             'dataset answers it; the data is fetched live, the answer is checked, and the search '
             'backtracks until it actually answers your question. '
             '<a href="how-it-works" style="color:#1a73e8">How it works ›</a> · <a href="life-of-a-query" style="color:#1a73e8">Life of a query ›</a> · <a href="techsoup" style="color:#1a73e8">TechSoup view ›</a></p>',
             '<p class="sub">A curated view for TechSoup and the nonprofits, libraries, and '
             'foundations it serves — validate an organization, measure the digital divide, read a '
             "nonprofit's finances, understand the communities it serves, and find funding. Ask in "
             'plain English; the answer is fetched live and cited. '
             '<a href="how-it-works" style="color:#1a73e8">How it works ›</a> · <a href="life-of-a-query" style="color:#1a73e8">Life of a query ›</a> · '
             '<a href="./" style="color:#1a73e8">‹ full data explorer</a></p>')
    .replace("fetch('sources')", "fetch('techsoup-sources')")
    .replace('placeholder="e.g. Is the American Red Cross a 501(c)(3)?"',
             'placeholder="e.g. Is Feeding America in good standing with the IRS?"')
    .replace("<h2 class=\"sh\">Example questions</h2>\n"
             "<p class=\"shsub\">Pick a theme, then click a question to run it live.</p>",
             "<h2 class=\"sh\">What can I ask?</h2>\n"
             "<p class=\"shsub\">Grouped by what a nonprofit or its funders need. Click any question to run it live.</p>")
    .replace('<h2 class="sh">Data sources</h2>',
             '<h2 class="sh">Sources behind this view</h2>'))


# --- per-source daily request cap ---------------------------------------------------------
# /ask is unauthenticated and every call spends model credits, so one loop can run up a bill.
# This caps how many questions a single source may ask per UTC day.
#
# Counts are per PROCESS and in memory: a restart clears them. That is a deliberate limit rather
# than an oversight — the cap exists to stop a runaway script, not a determined attacker, and
# persisting it would mean a write on every request. If it ever needs to survive restarts or span
# instances, this is the seam to put Redis behind.
# Running totals since process start, for GET /costs. Per-question numbers answer "what did that
# cost"; this answers "what has this instance spent", which is the one that shows up on a bill.
_TOTALS = {"questions": 0, "llm_calls": 0, "total_tokens": 0, "cost_usd": 0.0,
           "discovery_calls": 0, "discovery_tokens": 0, "discovery_cost_usd": 0.0,
           "by_stage": {}, "by_model": {}}
_TOTALS_LOCK = threading.Lock()
_TELEMETRY_LOCK = threading.Lock()
TELEMETRY_PATH = os.getenv("TELEMETRY_PATH", os.path.join(ROOT, "cache", "telemetry.jsonl"))

# A public endpoint that fans out into several provider calls needs a hard concurrency ceiling in
# addition to the daily quota. Rejecting quickly is safer than allowing an unbounded ThreadingHTTPServer
# queue to turn a traffic spike into provider spend and memory pressure.
MAX_CONCURRENT_QUERIES = max(1, int(os.getenv("MAX_CONCURRENT_QUERIES", "4")))
_QUERY_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES)


def _usage_from_messages(messages):
    """Usage is carried by the NLWS GeneratedAnswer frame. Query execution now runs on a worker so
    the handler thread cannot read its thread-local ledgers directly."""
    for m in reversed(messages or []):
        if m.get("message_type") == nlweb.NLWS and isinstance(m.get("content"), dict):
            return m["content"].get("usage"), m["content"].get("discovery_usage")
    return None, None


def _record_telemetry(ip, req, content, elapsed_ms, status="complete"):
    """Durable JSONL request telemetry. Questions are represented by a hash by default so the
    operational record is useful without silently retaining user text."""
    import hashlib
    content = content or {}
    question = req.get("query") or ""
    row = {"at": time.time(), "status": status, "latency_ms": round(elapsed_ms),
           "client": hashlib.sha256(str(ip).encode()).hexdigest()[:12],
           "question": hashlib.sha256(str(question).encode()).hexdigest()[:16],
           "intent": content.get("intent"), "attempts": content.get("attempts") or [],
           "evidence_kind": (content.get("evidence") or {}).get("kind"),
           "answer_renderer": content.get("answer_renderer"),
           "usage": content.get("usage"), "discovery_usage": content.get("discovery_usage")}
    try:
        os.makedirs(os.path.dirname(TELEMETRY_PATH), exist_ok=True)
        with _TELEMETRY_LOCK, open(TELEMETRY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
    except OSError:
        pass


def _accumulate(usage, discovery):
    with _TOTALS_LOCK:
        _TOTALS["questions"] += 1
        for k in ("llm_calls", "total_tokens"):
            _TOTALS[k] += (usage or {}).get(k, 0)
        _TOTALS["cost_usd"] += (usage or {}).get("cost_usd", 0.0)
        _TOTALS["discovery_calls"] += (discovery or {}).get("llm_calls", 0)
        _TOTALS["discovery_tokens"] += (discovery or {}).get("total_tokens", 0)
        _TOTALS["discovery_cost_usd"] += (discovery or {}).get("cost_usd", 0.0)
        for field in ("by_stage", "by_model"):
            for k, v in ((usage or {}).get(field) or {}).items():
                b = _TOTALS[field].setdefault(k, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
                b["calls"] += v.get("calls", 0)
                b["tokens"] += v.get("tokens", 0)
                b["cost_usd"] += v.get("cost_usd", 0.0)


ASK_LIMIT_PER_DAY = int(os.getenv("ASK_LIMIT_PER_DAY", "200"))     # 0 disables the cap
TRUST_PROXY = os.getenv("TRUST_PROXY", "0").lower() in ("1", "true", "yes")
_QUOTA = {}                                     # ip -> [utc_day, count]
_QUOTA_LOCK = threading.Lock()


def _client_ip(handler):
    """The address to bill a request to.

    X-Forwarded-For is only consulted when TRUST_PROXY says something in front of us sets it.
    Trusting it unconditionally would make the cap trivially bypassable: the header is
    client-supplied, so anyone could send a fresh value per request and get a fresh quota. When a
    proxy IS trusted, the LAST entry is the one it appended (the peer that actually connected to
    it); earlier entries came from the client and are spoofable."""
    if TRUST_PROXY:
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff:
            ip = xff.split(",")[-1].strip()
            if ip.count(":") == 1:              # strip a :port that some proxies append
                ip = ip.split(":")[0]
            if ip:
                return ip
    return handler.client_address[0]


def _quota_check(ip):
    """(allowed, used, seconds_until_reset). Counts every ask, including ones that fail — a
    refused question still paid for its classification and re-rank."""
    if ASK_LIMIT_PER_DAY <= 0:
        return True, 0, 0
    now = time.time()
    day = int(now // 86400)
    reset_in = int((day + 1) * 86400 - now)
    with _QUOTA_LOCK:
        rec = _QUOTA.get(ip)
        if rec is None or rec[0] != day:
            if len(_QUOTA) > 50_000:            # bound the table; stale days are dead weight
                for k in [k for k, v in _QUOTA.items() if v[0] != day]:
                    _QUOTA.pop(k, None)
            rec = [day, 0]
            _QUOTA[ip] = rec
        if rec[1] >= ASK_LIMIT_PER_DAY:
            return False, rec[1], reset_in
        rec[1] += 1
        return True, rec[1], reset_in




ARD_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARD entries</title>
<style>
 body{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:960px;margin:36px auto;padding:0 20px;color:#1a1a1a}
 h1{font-size:1.5em;margin:0 0 4px} a{color:#1a73e8;text-decoration:none} a:hover{text-decoration:underline}
 .sub{color:#5f6368;margin:0 0 20px}
 .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0}
 select,input{font:inherit;padding:7px 9px;border:1px solid #dadce0;border-radius:8px}
 input{flex:1;min-width:220px}
 .meta{color:#5f6368;font-size:.9em;margin:8px 0}
 .row{border:1px solid #e8eaed;border-radius:10px;padding:10px 13px;margin:8px 0;cursor:pointer;background:#fff}
 .row:hover{border-color:#1a73e8;background:#f8fbff}
 .row h3{margin:0 0 3px;font-size:.98em}
 .id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78em;color:#80868b;word-break:break-all}
 .desc{color:#3c4043;font-size:.88em;margin:4px 0 0}
 .q{display:inline-block;background:#f1f3f4;border-radius:11px;padding:1px 9px;margin:4px 4px 0 0;font-size:.79em;color:#3c4043}
 .pager{display:flex;gap:8px;align-items:center;justify-content:center;margin:18px 0;flex-wrap:wrap}
 .pager button{font:inherit;padding:6px 12px;border:1px solid #dadce0;background:#fff;border-radius:8px;cursor:pointer}
 .pager button:disabled{opacity:.4;cursor:default}
 pre{background:#0d1117;color:#c9d1d9;padding:14px;border-radius:10px;overflow:auto;font-size:.82em;line-height:1.45}
 .back{display:inline-block;margin-bottom:12px}
 .lbl{font-size:.78em;text-transform:uppercase;letter-spacing:.05em;color:#80868b;margin:16px 0 5px}
 .api{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78em;color:#5f6368;background:#f8f9fa;border:1px solid #e8eaed;border-radius:8px;padding:7px 11px;margin:0 0 6px}
 .api b{color:#1a73e8;font-weight:600}
 .pale{color:#9aa0a6;font-weight:400;text-transform:none;letter-spacing:0}
</style></head><body>
<h1>ARD entries</h1>
<p class="sub">Every table is described once as an <b>OKF</b> document — markdown with actionable
frontmatter — and served by the <b>ARD</b> Agent Finder. This page is an ARD <i>client</i>: it
enumerates the registry over the same API an agent would.
<a href="ard/manifest" target="_blank">/.well-known/ard.json</a> ·
<a href="./">‹ back to the query UI</a></p>
<div id="api" class="api"></div>
<div id="view"></div>
<script>
 function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
   return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
 function qp(){var o={},p=new URLSearchParams(location.search);p.forEach(function(v,k){o[k]=v});return o}
 function go(o){var p=new URLSearchParams(o);history.pushState({},'', 'ard?'+p);render()}
 var SOURCES=[];
 function render(){
   var o=qp();
   if(o.id) return renderEntry(o.id);
   var src=o.source||(SOURCES[0]&&SOURCES[0].dir)||'sec-edgar';
   fetch('ard/list?source='+encodeURIComponent(src)+'&page='+(o.page||1)
        +'&per='+(o.per||50)+'&q='+encodeURIComponent(o.q||''))
     .then(function(r){return r.json()}).then(function(d){renderList(d)});
 }
 function api(call){var el=document.getElementById('api'); if(el)el.innerHTML='ARD call: <b>'+esc(call)+'</b>'}
 function renderList(d){
   api('GET /agents?publisher='+d.source+(d.query?'&q='+d.query:'')+'&pageSize='+d.per
       +(d.page>1?'&pageToken=\u2026':''));
   var opts=SOURCES.map(function(s){return '<option value="'+esc(s.dir)+'"'
     +(s.dir===d.source?' selected':'')+'>'+esc(s.dir)+' ('+s.count+')</option>'}).join('');
   var h='<div class="bar"><select id="src">'+opts+'</select>'
     +'<input id="q" placeholder="filter by title, description or example query…" value="'+esc(d.query)+'">'
     +'</div>';
   h+='<div class="meta">'+d.total.toLocaleString()+' entr'+(d.total===1?'y':'ies')
     +(d.query?' matching “'+esc(d.query)+'”':'')+' · page '+d.page+' of '+d.pages+'</div>';
   h+=d.entries.map(function(e){
     return '<div class="row" data-id="'+esc(e.identifier)+'">'
       +'<h3>'+esc(e.title)+'</h3><div class="id">'+esc(e.identifier)+'</div>'
       +(e.description?'<p class="desc">'+esc(e.description.slice(0,240))
         +(e.description.length>240?'…':'')+'</p>':'')
       +(e.queries||[]).map(function(q){return '<span class="q">'+esc(q)+'</span>'}).join('')
       +'</div>'}).join('') || '<p class="meta">no entries match.</p>';
   h+=pager(d);
   document.getElementById('view').innerHTML=h;
   document.getElementById('src').onchange=function(){go({source:this.value})};
   var qi=document.getElementById('q'), t;
   qi.oninput=function(){clearTimeout(t);var v=this.value;
     t=setTimeout(function(){go({source:d.source,q:v})},300)};
   [].forEach.call(document.querySelectorAll('.row'),function(r){
     r.onclick=function(){go({id:r.getAttribute('data-id')})}});
 }
 function pager(d){
   if(d.pages<=1) return '';
   function b(p,label,dis){return '<button '+(dis?'disabled':'')+' data-p="'+p+'">'+label+'</button>'}
   var h='<div class="pager">'+b(1,'« first',d.page===1)+b(d.page-1,'‹ prev',d.page===1)
     +'<span class="meta">page '+d.page+' / '+d.pages+'</span>'
     +b(d.page+1,'next ›',d.page===d.pages)+b(d.pages,'last »',d.page===d.pages)+'</div>';
   setTimeout(function(){[].forEach.call(document.querySelectorAll('.pager button'),function(bt){
     bt.onclick=function(){go({source:d.source,q:d.query,page:bt.getAttribute('data-p')})}})},0);
   return h;
 }
 function renderEntry(id){
   fetch('ard/entry?id='+encodeURIComponent(id)).then(function(r){return r.json()}).then(function(e){
     if(e.error){document.getElementById('view').innerHTML='<p>'+esc(e.error)+'</p>';return}
     api('GET /agents/entry?id='+e.identifier);
     var a=e.ard_entry||{}, fm=(a.data&&a.data.frontmatter)||{};
     // the entry as the API serves it, minus the inlined document — that is shown as markdown
     // below, and repeating it here as an escaped one-line string helps nobody read it
     var slim=JSON.parse(JSON.stringify(a));
     if(slim.data){slim.data={mediaType:(a.data||{}).mediaType,
        frontmatter:fm, content:'‹shown below›'};}
     var h='<a class="back" href="#" id="bk">‹ back to '+esc(e.source)+' entries</a>'
       +'<h1 style="font-size:1.25em">'+esc(a.displayName||e.identifier)+'</h1>'
       +'<div class="id">'+esc(e.identifier)+'</div>'
       +'<div class="lbl">the ARD entry <span class="pale">— GET /agents/entry?id='
       +esc(e.identifier)+'</span></div>'
       +'<pre>'+esc(JSON.stringify(slim,null,2))+'</pre>'
       +'<p><a href="ard-api-entry?id='+encodeURIComponent(e.identifier)+'" id="rawjson">'
       +'open the raw JSON ›</a></p>'
       +'<div class="lbl">the OKF document this entry is generated from</div>'
       +'<pre>'+esc(e.raw)+'</pre>'
       +'<div class="lbl">source access descriptor</div>'
       +'<p><a href="ard?id='+encodeURIComponent(e.access_doc)+'">'+esc(e.access_doc)+'</a>'
       +' — the endpoint and query operations every leaf in this source inherits</p>';
     document.getElementById('view').innerHTML=h;
     document.getElementById('bk').onclick=function(ev){ev.preventDefault();go({source:e.source})};
     var rj=document.getElementById('rawjson');
     if(rj)rj.setAttribute('href','ard/entry?id='+encodeURIComponent(e.identifier));
   });
 }
 window.onpopstate=render;
 fetch('ard/publishers').then(function(r){return r.json()}).then(function(d){
   SOURCES=d.publishers||[];render()});
</script></body></html>"""



# --- the NLWeb query, over this engine -------------------------------------------------------
def _nlweb_text(ev):
    """One engine progress event as a line of NLWeb intermediate_message prose."""
    k = ev.get("kind")
    if k == "status":
        return f"{ev.get('icon','')} {ev.get('msg','')}".strip()
    if k == "plan":
        bits = [b for b in (f"entity {ev.get('entity')}" if ev.get("entity") else "",
                            f"measure {ev.get('attribute')}" if ev.get("attribute") else "",
                            f"period {ev.get('period')}" if ev.get("period") else "") if b]
        return "🧭 " + " · ".join(bits) + f" · scanning {len(ev.get('sources') or [])} sources"
    if k == "candidates":
        return "📚 ARD returned " + str(len(ev.get("items") or [])) + " candidate tables"
    if k == "plan_chosen":
        return "🧭 " + (ev.get("summary") or ev.get("verdict") or "")
    if k == "resolve":
        return f"🧩 resolved “{ev.get('mention','')}” → {ev.get('label','')}"
    return ""


def run_nlweb(req):
    """Generator of NLWeb messages for one query. The engine already narrates itself through
    `_say`; those events become intermediate_message frames, so the protocol carries the same
    play-by-play the native UI always showed rather than going quiet until the answer lands."""
    st = nlweb.Stream(req.get("conversation_id"))
    yield st.message(nlweb.BEGIN, "", "system")

    events = queue.Queue()
    cancelled = threading.Event()
    deadline = time.monotonic() + int(os.getenv("QUERY_TIMEOUT_SECONDS", "180"))

    def work():
        runtime.bind(cancelled, deadline)
        _EMIT.cb = lambda ev: events.put(("event", ev))
        try:
            events.put(("done", run(req["query"], sites=req.get("sites") or None,
                                    assumptions=req.get("assumptions") or None,
                                    on_ambiguity=req.get("on_ambiguity") or "answer")))
        except SystemExit as e:                        # an honest refusal, not a crash
            events.put(("error", str(e)))
        except driver.SourceRateLimitError as e:       # temporary publisher condition, shown verbatim
            events.put(("error", str(e)))
        except Exception as e:
            # A refusal above is expected and needs no stack. Reaching HERE is a bug, and the
            # client only ever sees "AttributeError: 'str' object has no attribute 'items'",
            # which names neither the file nor the line. Two such crashes have now been
            # reported from the deployment and could not be located from the outside because
            # the traceback was discarded here. Log it; keep sending the client the summary.
            print(f"[query failed] {req.get('query', '')[:200]!r}", file=sys.stderr)
            traceback.print_exc()
            events.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            _EMIT.cb = None

    worker = threading.Thread(target=work, name="resource-raiser-query", daemon=True)
    worker.start()
    result, error = None, None
    try:
        while result is None and error is None:
            try:
                kind, payload = events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                cancelled.set()
                error = "query deadline exceeded"
                break
            if kind == "event":
                line = _nlweb_text(payload)
                if line:
                    yield st.message(nlweb.INTERMEDIATE, line, "system")
            elif kind == "done":
                result = payload
            else:
                error = payload
    finally:
        if result is None:
            cancelled.set()

    if error or not result:
        yield st.message(nlweb.ERROR, error or "no answer", "system")
        yield st.message(nlweb.END, "", "system")
        return

    site_of = {c["title"]: c.get("publisher") for c in (result.get("candidates") or [])}
    items = [nlweb.item(c) for c in (result.get("candidates") or [])
             if (c.get("score") or 0) >= req.get("min_score", 0)][:req.get("max_results", 10)]
    # the table that actually answered belongs at the head of the list, whatever discovery ranked
    src = result.get("source") or {}
    if src.get("identifier"):
        chosen = nlweb.item({"identifier": src["identifier"], "title": src.get("title"),
                             "publisher": src.get("publisher"), "score": 100,
                             "description": result.get("plan", ""),
                             "schema_object": driver.frontmatter(src["identifier"]) or {}})
        items = [chosen] + [i for i in items if i["url"] != chosen["url"]]
        items = items[:max(1, req.get("max_results", 10))]
    if items:
        yield st.message(nlweb.RESULT, items)

    if req.get("mode") != "list":
        # `answer` and `items` are the GeneratedAnswer contract; the rest are additive fields a
        # strict NLWeb client ignores and this engine's own UI uses to render rankings, record
        # lists and the cost report. One protocol, no second endpoint.
        clarification = result.get("clarification") if result.get("status") == "needs_clarification" else None
        content = {
            "@type": "ClarificationRequest" if clarification else "GeneratedAnswer",
            "items": items,
            "status": result.get("status") or "answered",
            "shape": result.get("shape"),
            "plan": result.get("plan"),
            "data": result.get("data"),
            "usage": result.get("usage"),
            "discovery_usage": result.get("discovery_usage"),
            "intent": result.get("intent"),
            "attempts": result.get("attempts") or [],
            "evidence": result.get("evidence"),
            "answer_renderer": result.get("answer_renderer"),
        }
        if clarification:
            content.update({"question": clarification.get("question"),
                            "original_query": result.get("question"),
                            "options": clarification.get("options") or []})
        else:
            content["answer"] = result.get("answer") or ""
        yield st.message(nlweb.NLWS, content)
    if req.get("debug"):
        yield st.message(nlweb.INTERMEDIATE,
                         json.dumps({"shape": result.get("shape"), "plan": result.get("plan"),
                                     "usage": result.get("usage"),
                                     "discovery_usage": result.get("discovery_usage"),
                                     "data": result.get("data")})[:20000], "system")
    yield st.message(nlweb.COMPLETE, "", "system")
    yield st.message(nlweb.END, "", "system")


HOW_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How Resource Raiser works</title>
<style>
 body{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#1a1a1a}
 h1{font-size:1.5em;margin:0 0 6px} h2{font-size:1.05em;margin:30px 0 8px}
 p{margin:10px 0} a{color:#1a73e8;text-decoration:none} a:hover{text-decoration:underline}
 .sub{color:#5f6368;margin-bottom:22px}
 pre{background:#0d1117;color:#c9d1d9;padding:14px 16px;border-radius:10px;overflow:auto;font-size:.82em;line-height:1.5}
 table{border-collapse:collapse;width:100%;margin:10px 0;font-size:.92em}
 th,td{text-align:left;padding:7px 12px 7px 0;border-bottom:1px solid #e8eaed;vertical-align:top}
 th{color:#5f6368;font-weight:600}
 code{background:#f1f3f4;border-radius:4px;padding:1px 5px;font-size:.88em}
 .note{color:#5f6368;font-size:.92em}
</style></head><body>
<h1>How it works</h1>
<p class="sub">Describe each dataset once; discover it by meaning; fetch from the source at
question time. <a href="./">‹ back</a></p>

<h2>Control flow</h2>
<p>One question moves through six steps. Each can send it back a step, which is why a wrong
first guess degrades into a slower answer instead of a wrong one.</p>
<pre>question
  │
  ├─ classify    what entity, what measure, what SHAPE (point, ranking, ratio, timeseries…)
  ├─ discover    ARD: embed the question, retrieve candidate tables, re-rank them
  ├─ plan        does a candidate's declared capability support that shape?
  │                 no  → refuse here, before any request is made
  ├─ fetch       one generic accessor fills the URL template from the OKF descriptor
  ├─ check       is this record actually about what was asked?
  │                 no  → backtrack: next table, next entity, next period
  └─ synthesize  answer grounded in the returned record, quoting its figure and source</pre>
<p>The planning step is the unusual one. A source that lists one nonprofit's grants can compare
two named organizations but cannot rank the whole population — so a ranking question over it is
refused, not approximated. Refusing costs one classification; guessing costs credibility.</p>

<h2>Data flow</h2>
<p>Nothing is ingested. The only thing this system stores is <em>descriptions</em>:</p>
<pre>OKF descriptors  ──embed──▶  ARD index     (~10,400 tables, ~60 MB of vectors)
                                  │
question ─────────────────────────┘  picks ONE table
                                  │
                                  ▼
                        the source's own API  ──▶  answer
                        (SEC, Census, Treasury, CDC, IRS, …)</pre>
<p>The record that answers your question is fetched from the publisher, in that moment, and
discarded. There is no copy to refresh and no schema to migrate. Adding a source means adding a
folder with a Markdown file in it — no per-source query code.</p>

<h2>Why not a warehouse</h2>
<p>The usual approach — Data Commons, a lakehouse, any central warehouse — normalizes many
sources into one schema and loads the data into one place. That buys real things: arbitrary joins,
fast aggregates, one query language. It costs real things too.</p>
<table>
<tr><th></th><th>Warehouse / Data Commons</th><th>This</th></tr>
<tr><td>Unit of work</td><td>a pipeline per source</td><td>a description per source</td></tr>
<tr><td>Schema</td><td>normalize everything up front</td><td>keep each source's own</td></tr>
<tr><td>Data location</td><td>copied into the centre</td><td>stays at the publisher</td></tr>
<tr><td>Freshness</td><td>as of the last load</td><td>as of the request</td></tr>
<tr><td>Adding a source</td><td>model it, map it, backfill it</td><td>write one document</td></tr>
<tr><td>Good at</td><td>joins and aggregates over everything</td><td>breadth, currency, provenance</td></tr>
<tr><td>Bad at</td><td>long tail — the 8,000th field is never worth a pipeline</td><td>cross-source joins, population scans</td></tr>
</table>
<p>The trade is deliberate. Normalization is what makes the long tail unaffordable: nobody funds a
pipeline for the 8,096th us-gaap concept, so it never arrives. A description is cheap enough to
write for all of them, which is why this covers ~10,400 measures rather than a curated few.</p>
<p>The cost is equally real. Cross-source joins are the warehouse's home ground and this system's
weak spot, and questions over a whole population need a source that can scan one — which is
exactly what the planner checks before it answers.</p>

<h2>The exception that shows the rule</h2>
<p>One source is not live: the IRS 990 grant graph, ~7.8 M funder→recipient edges. The IRS
publishes no query API for it, only bulk filings, so there is nothing to call at question time and
the edges are built once into a database. Every other source stayed live because its publisher
offered a way to ask.</p>

<p class="note">Descriptors are <a href="https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf">OKF</a>
documents; discovery speaks <a href="https://agenticresourcediscovery.org/">ARD</a>; the query
interface is <a href="https://github.com/nlweb-ai/NLWeb">NLWeb</a>.
<a href="ard">Browse the descriptors ›</a></p>
<p class="note">This page is the overview. <a href="life-of-a-query">The life of a query ›</a>
follows one question all the way through — every branch, every backtrack, and where the boundary
of what can be asked actually falls.</p>
</body></html>"""


def serve(port, ready=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer

    class H(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _html(self, page):
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # split the query string off before routing — /ard/list?source=… must still match
            # "/ard/list", which a raw self.path comparison never does
            p = urllib.parse.urlparse(self.path).path.rstrip("/")
            if p in ("", "/"):
                return self._html(PAGE)
            if p == "/techsoup":
                return self._html(TECHSOUP_PAGE)
            if p == "/healthz":
                # Liveness for a load balancer / App Service health check: is the index loaded and
                # is the finder reachable? Deliberately does NOT call the LLM — a health probe that
                # costs money per poll is a bill, not a health check.
                health = {}
                try:
                    health = ard_client.health()
                except Exception:
                    pass
                finder = bool(health.get("ok"))
                n = int(health.get("entries") or 0)
                code = 200 if (n and finder) else 503
                return self._json(code, {"ok": code == 200, "tables": n, "agent_finder": finder})
            if p == "/costs":
                with _TOTALS_LOCK:
                    t = json.loads(json.dumps(_TOTALS))     # snapshot under the lock
                n = max(1, t["questions"])
                t["avg_cost_per_question_usd"] = round((t["cost_usd"] + t["discovery_cost_usd"]) / n, 6)
                t["combined_cost_usd"] = round(t["cost_usd"] + t["discovery_cost_usd"], 6)
                t["cost_usd"] = round(t["cost_usd"], 6)
                t["discovery_cost_usd"] = round(t["discovery_cost_usd"], 6)
                for field in ("by_stage", "by_model"):
                    t[field] = {k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                                for k, v in sorted(t[field].items(),
                                                   key=lambda kv: -kv[1]["cost_usd"])}
                t["note"] = ("since process start; discovery_* is the ARD Agent Finder, a separate "
                             "service, and is not included in cost_usd")
                return self._json(200, t)
            if p == "/ask":
                return self._ask(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            if p == "/sites":
                # an OKF source IS an NLWeb site: the corpus a result came from
                return self._json(200, {"message_type": "sites",
                                        "sites": [s["dir"] for s in _sources_catalog()]})
            if p == "/health":
                return self._json(200, {"status": "ok"})
            if p in ("/how-it-works", "/how"):
                return self._html(HOW_PAGE)
            if p in ("/life-of-a-query", "/loq"):
                # Rendered from the repository Markdown on each request, so the page cannot drift
                # from the document in the tree. Absent in a deployment that did not ship it.
                doc = docpage.markdown_page(
                    "LIFE_OF_A_QUERY.md", "The life of a query",
                    "How one question becomes an answer, a clarification, or a refusal \u2014 and "
                    "where the boundary of what can be asked actually falls.")
                if doc is None:
                    return self._json(404, {"error": "LIFE_OF_A_QUERY.md is not deployed"})
                return self._html(doc)
            if p in ("/ard", "/ard/"):
                return self._html(ARD_PAGE)
            if p == "/ard/publishers":
                return self._json(200, {"publishers": _ard_publishers()})
            if p == "/ard/manifest":
                return self._json(200, ard_client.manifest())
            if p == "/ard/list":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try:
                    page = max(1, int((qs.get("page") or ["1"])[0] or 1))
                    per = max(1, min(int((qs.get("per") or ["50"])[0] or 50), 100))
                except (TypeError, ValueError):
                    return self._json(400, {"error": "page and per must be integers"})
                return self._json(200, _ard_list(
                    (qs.get("source") or [""])[0],
                    page, per,
                    (qs.get("q") or [""])[0]))
            if p == "/ard/entry":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                e = _ard_entry((qs.get("id") or [""])[0])
                return self._json(200, e) if e else self._json(404, {"error": "no such entry"})
            if p == "/sources":
                return self._json(200, {"sources": _sources_catalog(), "tabs": EXAMPLE_TABS})
            if p == "/techsoup-sources":
                # only the sources this curated view uses, plus the TechSoup-organized tabs
                dirs = {d for t in TECHSOUP_TABS for d in t["dirs"]}
                srcs = [s for s in _sources_catalog() if s["dir"] in dirs]
                return self._json(200, {"sources": srcs, "tabs": TECHSOUP_TABS})
            self._json(404, {"error": "not found"})

        def _ask(self, params):
            """The one query contract: NLWeb. Streams by default; `streaming=false` returns the
            same messages as a single JSON document, which is what NLWeb clients expect."""
            req = nlweb.parse_request(params)
            request_started = time.monotonic()
            if not req["query"]:
                return self._json(400, {"error": "missing 'query'"})
            # An unreadable binding must not be ignored. Answering anyway would resolve a
            # clarification to the wrong interpretation and state it with full confidence.
            if req.get("assumptions_error"):
                return self._json(400, {"error": req["assumptions_error"]})
            ip = _client_ip(self)
            allowed, used, reset_in = _quota_check(ip)
            if not allowed:
                self.send_response(429)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", str(reset_in))
                body = json.dumps({"error": f"daily limit reached: {ASK_LIMIT_PER_DAY} queries per "
                                            f"day per source", "retry_after_seconds": reset_in}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if not _QUERY_SLOTS.acquire(blocking=False):
                return self._json(503, {"error": "server is at its concurrent query limit"})

            if not req["streaming"]:
                try:
                    msgs = list(run_nlweb(req))
                    usage, discovery = _usage_from_messages(msgs)
                    _accumulate(usage, discovery)
                    content = next((m.get("content") for m in reversed(msgs)
                                    if m.get("message_type") == nlweb.NLWS), {})
                    _record_telemetry(ip, req, content,
                                      (time.monotonic() - request_started) * 1000,
                                      ("needs_clarification" if content.get("status") == "needs_clarification"
                                       else "complete"))
                    return self._json(200, {"messages": msgs})
                finally:
                    _QUERY_SLOTS.release()

            self.send_response(200)
            self._cors()
            for k, v in nlweb.SSE_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            usage = discovery = None
            answer_content = {}
            try:
                for m in run_nlweb(req):
                    if m.get("message_type") == nlweb.NLWS and isinstance(m.get("content"), dict):
                        answer_content = m["content"]
                        usage = m["content"].get("usage")
                        discovery = m["content"].get("discovery_usage")
                    self.wfile.write(nlweb.encode(m, named=req["named_events"]))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass                                   # client hung up mid-stream; nothing to do
            finally:
                _accumulate(usage, discovery)
                _record_telemetry(ip, req, answer_content,
                                  (time.monotonic() - request_started) * 1000,
                                  (("needs_clarification" if answer_content.get("status") ==
                                    "needs_clarification" else "complete") if answer_content else
                                   "disconnected-or-error"))
                _QUERY_SLOTS.release()

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if path != "/ask":
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                return self._json(400, {"error": "invalid Content-Length"})
            max_body = int(os.getenv("HARNESS_MAX_BODY", "65536"))
            if n < 0 or n > max_body:
                return self._json(413, {"error": f"request body exceeds {max_body} bytes"})
            try:
                params = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON body"})
            self._ask(params if isinstance(params, dict) else {})

        def log_message(self, *a):
            pass

    # Loopback by default — the /ask endpoint spends LLM credits per call and has no auth, so it
    # is not something to bind to the world without saying so. Set BIND_HOST to expose it.
    host = os.getenv("HARNESS_BIND_HOST", "127.0.0.1")
    print(f"Query harness on http://{host}:{port}/  (POST /ask)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  NOTE: bound beyond loopback. /ask is unauthenticated and each call costs money — "
              "put it behind a proxy that terminates TLS and authenticates.")
    server = HTTPServer((host, port), H)
    if ready:
        ready(server)
    def _stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()


_STEP_ORDER = ["classify", "resolve-entity", "resolve-concept", "check", "synthesize", "other"]
_STEP_LABEL = {"classify": "classify the question", "resolve-entity": "resolve the entity",
               "resolve-concept": "resolve the measure", "check": "check the answer fits",
               "synthesize": "write the answer", "other": "other"}


def _print_cost_report(u, d, out=None):
    """Per-step cost report, in pipeline order. Goes to stderr so piping the JSON stays clean."""
    out = out or sys.stderr
    if not u:
        return
    print(f"\n{'step':24}{'calls':>7}{'tokens':>10}{'cost':>12}", file=out)
    print("-" * 53, file=out)
    tot_cost = tot_tok = tot_calls = 0
    for k in _STEP_ORDER:
        v = (u.get("by_stage") or {}).get(k)
        if not v:
            continue
        tot_cost += v["cost_usd"]; tot_tok += v["tokens"]; tot_calls += v["calls"]
        print(f"{_STEP_LABEL.get(k, k):24}{v['calls']:>7}{v['tokens']:>10,}"
              f"{'$' + format(v['cost_usd'], '.5f'):>12}", file=out)
    if d.get("llm_calls"):
        print("-" * 53, file=out)
        print(f"{'discovery (agent finder)':24}{d['llm_calls']:>7}{d['total_tokens']:>10,}"
              f"{'$' + format(d['cost_usd'], '.5f'):>12}", file=out)
        tot_cost += d["cost_usd"]; tot_tok += d["total_tokens"]; tot_calls += d["llm_calls"]
    print("-" * 53, file=out)
    print(f"{'TOTAL':24}{tot_calls:>7}{tot_tok:>10,}{'$' + format(tot_cost, '.5f'):>12}", file=out)
    src = "billed by provider" if u.get("cost_source") == "provider" else "estimated from a price table"
    print(f"({src}; resolution steps are cached per process)", file=out)



def main(argv):
    if not llm.have_credentials():
        sys.exit(llm._NO_CREDS)
    if argv and argv[0] == "--serve":
        # --port wins; then PORT/WEBSITES_PORT, which is how App Service and most PaaS hosts tell
        # an app where to listen; then the local default.
        if "--port" in argv:
            port = int(argv[argv.index("--port") + 1])
        else:
            port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or 8099)
        return serve(port)
    res = run(" ".join(argv) or "How much did Apple spend on R&D in 2023?")
    print(json.dumps(res, indent=2))
    _print_cost_report(res.get("usage") or {}, res.get("discovery_usage") or {})


if __name__ == "__main__":
    main(sys.argv[1:])
