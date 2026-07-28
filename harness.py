#!/usr/bin/env python3
"""Query harness — the orchestrator a skill delegates to.

  question
    -> ARD Agent Finder (POST /search via ard_client)  : which data source/table?
    -> retrieve from that source (live)                : SEC concept, or any OKF source
    -> synthesize a cited answer

Run as a CLI or as a server the connectors skill calls:
  set -a; source /Users/rvguha/code/test/AskAgent/set_keys.sh; set +a
  python3 harness.py "How much did Apple spend on R&D in 2023?"     # one-shot (prints JSON)
  python3 harness.py --serve [--port 8099]                          # POST /ask {"question": ...}
"""
import os, sys, json, time, math, re
import driver, ard_client, planner, store
from core import Toolkit

ROOT = os.path.dirname(os.path.abspath(__file__))
TK = Toolkit()

# Live-progress channel. When a request is streaming (/ask_stream), _EMIT is set to a
# writer that pushes each event to the browser; otherwise _say is a no-op. The server is
# single-threaded, so one module-level callback is safe for the in-flight request.
import threading
_EMIT = None
_EMIT_LOCK = threading.Lock()          # parallel executors emit from worker threads; serialize the writes


def _say(kind, **data):
    cb = _EMIT
    if cb:
        try:
            with _EMIT_LOCK:
                cb({"kind": kind, **data})
        except Exception:
            pass


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


def discover(question):
    """Pass 1: extract the ENTITY, the entity-expunged ATTRIBUTE, and (via the entity's
    TYPE) which SOURCE(s) apply. Pass 2: match the attribute to fields within those sources."""
    _say("status", icon="🔍", msg="Reading your question…")
    src_list = "\n".join(f"- {d}: covers {t}" for d, t in SOURCE_TYPES.items())
    ctx = json.loads(TK.llm(
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
        "SOURCES:\n" + src_list, question, json_mode=True))
    ctx = _normalize_shape(ctx)
    # Robustness: the classifier sometimes drops the place from a "<measure> in <Place>" question
    # (leaving an empty entity). Recover it from the question so a place lookup doesn't fail with no geo.
    if ctx.get("type") == "place" and not (ctx.get("entity") or "").strip():
        m = re.search(r"\bin (?:the )?([A-Z][\w .,'&-]+?)\s*\??$", question)
        if m:
            ctx["entity"] = m.group(1).strip()
    sources = [s for s in (ctx.get("sources") or []) if s in SOURCE_TYPES] or list(SOURCE_TYPES)
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
    for h in ard_client.search(primary, k=8, sources=sources) + ard_client.search(secondary, k=4, sources=sources):
        if h["identifier"] not in seen:
            seen.add(h["identifier"])
            hits.append(h)
    _say("candidates", items=[{"title": h["title"], "score": h["score"], "publisher": h.get("publisher")}
                              for h in hits[:6]])
    return ctx, hits


def _geo_from_fips(keys):
    if keys.get("fips_place") and "-" in keys["fips_place"]:   # "SS-PPPPP" (else fall through to county)
        s, p = keys["fips_place"].split("-", 1)
        return f"place:{p}&in=state:{s}"
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
        except Backtrack as e:
            last = e
    raise Backtrack(f"no viable {name} ({last})")


# Per-process memo caches for values that are IDENTICAL across every backtrack attempt of one
# question — the ticker for a mention, and the resolved entity candidates. Without these the harness
# re-runs the same LLM + Wikidata calls dozens of times while exhausting candidates (the capex case).
_TICKER_CACHE = {}
_ENTITY_CACHE = {}


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
            'JSON {"order":[<indices>]}.\n' + listing, mention, json_mode=True)).get("order", [])
    except Exception:
        order = []
    order = [i for i in order if isinstance(i, int) and 0 <= i < len(cands)] or list(range(len(cands)))
    out = []
    for i in order[:3]:
        try:
            label, keys = resolver._claims(cands[i]["id"])
            out.append({"qid": cands[i]["id"], "label": label, "keys": keys})
        except Exception:
            pass
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


def _fetch(state, ctx):
    """Attempt one complete (field, entity, key, period) assignment. Raise Backtrack on any failure."""
    identifier = state["hit"]["identifier"]
    fm = driver.frontmatter(identifier)
    key, period = state.get("key"), state.get("period") or "latest"
    attribute, mention = ctx.get("attribute") or "", ctx.get("entity") or ""
    try:
        if fm.get("concept"):
            if key:
                return driver.fetch_metric(attribute, cik=key, period=period, log=False)
            if mention not in _TICKER_CACHE:                 # same mention on every backtrack — resolve once
                _TICKER_CACHE[mention] = json.loads(TK.llm(
                    'JSON {"ticker":"<US stock ticker or empty>"}.', mention, json_mode=True)).get("ticker")
            ticker = _TICKER_CACHE[mention]
            if not ticker:
                raise Backtrack("no ticker")
            return driver.fetch_metric(attribute, ticker, period, log=False)
        if fm.get("classification"):
            import nonprofit
            if not key:
                raise Backtrack("no nonprofit key")
            return nonprofit.classify(key)
        if fm.get("field"):
            import nonprofit
            if not key:
                raise Backtrack("no nonprofit key")
            return nonprofit.fetch_np(fm["field"], key, period)
        if fm.get("bmf"):
            import nonprofit
            if not key:
                raise Backtrack("no nonprofit key")
            return nonprofit.bmf(fm["bmf"], key)
        if fm.get("profile"):
            import orgprofile as profile
            if not key:
                raise Backtrack("no wikidata qid")
            return profile.fetch(fm["profile"], key, (state.get("entity") or {}).get("label"))
        if fm.get("scorecard"):
            import college
            return college.fetch(fm["scorecard"], key or mention)
        if fm.get("fema"):
            import fema
            return fema.fetch(key or mention)
        if fm.get("variable"):
            geo = _resolve_geo(mention) if key == "__native__" else key
            if not geo:
                raise Backtrack("no geo")
            def _jam(x):
                try:
                    return float(x) <= -100000000                # ACS jam sentinels are large negatives
                except (TypeError, ValueError):
                    return False
            arr = driver.accessor(identifier, "acs", geo=geo)
            if not isinstance(arr, list) or len(arr) < 2:
                raise Backtrack("no census row")
            val, var = arr[1][1], (fm.get("get") or fm["variable"])
            # ACS Data Profile quirk: for a PERCENT row the value lives in the *PE* column and the *E*
            # column is -888888888 ("not applicable"). If the picked estimate is that sentinel, read the
            # percent sibling — otherwise a poverty/unemployment RATE looks like missing data and the
            # search backtracks forever over a value that is simply in the other column.
            if str(val).strip() == "-888888888" and var.endswith("E") and not var.endswith("PE"):
                pe = var[:-1] + "PE"
                a2 = driver.accessor(identifier, "acs", geo=geo, get=pe)
                if isinstance(a2, list) and len(a2) >= 2 and not _jam(a2[1][1]):
                    val, var = a2[1][1], pe
            if _jam(val):
                raise Backtrack("jam null")
            return {"place": arr[1][0], "metric": fm["title"].split(" — US Census")[0],
                    "variable": var, "value": val, "source": "US Census ACS (did:web:census.gov)"}
        if fm.get("measureid"):
            arr = driver.accessor(identifier, "by_measure", measureid=fm["measureid"], place=key)
            row = next((r for r in arr if r.get("data_value")), None) if isinstance(arr, list) else None
            if not row:
                raise Backtrack("no cdc row")
            return {"place": row.get("locationname"), "measure": fm["title"].split(" — CDC")[0],
                    "value": row.get("data_value"), "unit": row.get("data_value_unit"),
                    "source": "CDC PLACES (did:web:cdc.gov)"}
        if fm.get("tfield"):
            q = f"fields={fm['tfield']},record_date&sort=-record_date&page[size]=1"
            if fm.get("filter"):
                q += f"&filter={fm['filter']}"
            rows = (driver.accessor(identifier, "get", query=q) or {}).get("data", [])
            if not rows:
                raise Backtrack("no treasury data")
            rec = {"metric": fm["title"], "value": rows[0].get(fm["tfield"]),
                   "as_of": rows[0].get("record_date"), "source": "US Treasury FiscalData (did:web:treasury.gov)"}
            if fm.get("filter") and ":eq:" in fm["filter"]:
                rec["series"] = fm["filter"].split(":eq:")[-1]   # the dimension value, e.g. "Euro Zone-Euro"
            return rec
        if fm.get("search"):
            s = fm["search"]
            val = (key or mention) if s["want"] == "organization" else attribute
            if not val:
                raise Backtrack("no search term")
            cap = (planner.capabilities(identifier) or {}).get(s["operation"], {})
            page = cap.get("page") or {}

            def _pull(**extra):
                r = driver.accessor(identifier, s["operation"], **{s["arg"]: val, **extra})
                for part in s["extract"].split("."):
                    r = r[int(part)] if isinstance(r, list) else r.get(part, [])
                return r if isinstance(r, list) else []

            if page.get("complete_for") == "entity" and page.get("offset_param"):
                # ENTITY-scoped completeness: this org's own records fit under the offset ceiling, so
                # page them all. Without this the "total" is just the largest N projects — Johns
                # Hopkins reads $208M instead of $969M, and every threshold comparison is wrong.
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
            out = {"query": val, "source": fm.get("title")}
            if isinstance(res, list):
                rows = [r for r in res if isinstance(r, dict)]
                total = sum(_amount(r) for r in rows)        # compute totals HERE, not in the LLM
                out["record_count"] = len(rows)
                if total:
                    out["total_usd"] = round(total, 2)
                    out["total_usd_display"] = "${:,.0f}".format(total)
                # Completeness is DECLARED, not guessed: a capped page is a partial total. Propagating
                # it matters most for joins — dividing a truncated numerator by a complete denominator
                # is the characteristic way a cross-source join produces a confident wrong number.
                out["complete"] = bool(page.get("complete")) or page.get("complete_for") == "entity"
                if not out["complete"]:
                    out["coverage"] = (f"total is across the {len(rows)} award records returned by this "
                                       f"query, not every award the organization has received")
                out.update(_identity_scope(rows, fm.get("identity") or {}))   # scope a name-matched result
                res = [{k: v for k, v in r.items() if not (isinstance(v, str) and len(v) > 240)}
                       for r in rows][:8]                    # drop bulky prose (abstracts etc.)
            out["results"] = res
            return out
    except SystemExit as e:
        raise Backtrack(str(e))
    raise Backtrack("no structured retrieval for this source")


def _answers(question, data):
    """Acceptance test at the goal of the search: is this record ABOUT the right thing for the question?
    This is a ROUTING check, not a fact-check — it turns backtracking from 'no data' into 'no WRONG
    data' by matching the record's qualifiers (measure, unit, currency, place/entity) to the question.

    It must NOT judge the value itself: the model's own world-knowledge is wrong about magnitudes,
    exchange-rate direction, and 'future' dates (its training cutoff makes recent data look fake), so
    letting it fact-check the number causes false rejections of correct answers. Fail-open on error."""
    try:
        v = json.loads(TK.llm(
            "You route data: decide whether the DATA record is ABOUT the right thing for the QUESTION. "
            "Accept when its MEASURE, UNIT, CURRENCY, and PLACE/ENTITY match what the question asks. "
            "Reject ONLY for a clear mismatch in one of those: a different measure (e.g. 'intragovernmental "
            "holdings' when the total national debt was asked), a wrong unit (a total amount when a "
            "per-share value or a percentage/rate was asked, or vice versa), a different named currency, or "
            "a different place/entity (a broader containing area used as a proxy for a place is fine). "
            "CRUCIAL: do NOT judge the numeric VALUE in any way — do not consider whether it seems too "
            "large or small, whether an exchange rate looks inverted, or whether a date is recent, old, or "
            "in the future. Treat the value and its date as authoritative and current. Judge only WHAT the "
            "record is about. Bias strongly toward ACCEPT: if the record names the same currency, place, or "
            "measure the question asks about — even inside a longer official title (e.g. 'Treasury Reporting "
            "Rates of Exchange: Euro Zone-Euro' answers a euro exchange-rate question) — ACCEPT. Reject only "
            "when you are CONFIDENT it is a different currency/place/measure (e.g. China-Renminbi when the "
            'euro was asked). When in doubt, ACCEPT. Return JSON {"ok": true|false, "why": "<short reason>"}.',
            json.dumps({"question": question, "data": data}), json_mode=True))
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
                             "how many grant", "average grant", "how much grant money was", "total grant")):
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
                    "unit": d.get("unit"), "period": d.get("period"), "source": r.get("source")}
        except (SystemExit, Backtrack) as e:
            return {"interpretation": interp, "value": None, "error": str(e)}
        except Exception as e:
            return {"interpretation": interp, "value": None, "error": str(e)[:80]}

    # Run interpretations concurrently with a hard deadline. Some measures are genuinely
    # unavailable for the entity (a company has no clean employee-count concept) and backtrack
    # for a long time — the deadline stops one slow interpretation from blocking the rest.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ex = ThreadPoolExecutor(max_workers=min(4, len(interps)))
    futs = {ex.submit(one, i): i for i in interps}
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
    got = [a for a in answers if isinstance(a.get("value"), (int, float))]
    if not got:
        raise SystemExit(f"'{ctx.get('attribute')}' is ambiguous and none of its interpretations "
                         f"({', '.join(interps)}) could be answered")
    return {"question": question, "ambiguous": True, "attribute": ctx.get("attribute"),
            "entity": ent, "interpretations": answers,
            "source": " · ".join(dict.fromkeys(a.get("source") or "?" for a in got))}


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
            series = list(ex.map(one_year, yrs))               # ex.map preserves input order
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
        series = list(ex.map(one_sub, subs))                   # preserves order
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
    steps = [
        ("hit", lambda s: hits),
        ("entity", lambda s: _entity_options(ctx.get("entity"), ctx.get("type"))),
        ("key", lambda s: _key_options(s, ctx)),
        ("period", lambda s: ([p, "latest"] if p != "latest" else ["latest"])),
    ]

    attempts = [0]
    MAX_ATTEMPTS = 40                       # 3 entities x 2 periods x a couple keys is the honest ceiling;
                                            # beyond it the search is looping, not exploring — stop cleanly.

    def goal(s):
        if attempts[0] >= MAX_ATTEMPTS:
            raise SystemExit("no source could answer this after exhausting the ranked candidates "
                             "(the requested data may not be published yet).")
        attempts[0] += 1
        ent = (s.get("entity") or {}).get("label")
        _say("status", icon="📥", msg=f"Fetching live from “{s['hit']['title']}”"
             + (f" for {ent}…" if ent else "…"))
        data = _fetch(s, ctx)
        _say("status", icon="🔎", msg="Checking the result actually answers your question…")
        ok, why = _answers(question, data)
        if not ok:
            _say("status", icon="↩️", msg=f"Wrong table ({why}) — backtracking to the next candidate…")
            raise Backtrack(f"answer rejected: {why}")
        _say("status", icon="✅", msg="Result checks out — composing the grounded answer…")
        return {**s, "_data": data}

    try:
        state = _solve(steps, goal, {})
    except Backtrack as e:
        raise SystemExit(f"no source could answer: {e}")
    return ctx, hits, state["hit"], hits.index(state["hit"]) + 1, state["_data"], state


def retrieve_for(question):
    """Discover + retrieve for one sub-question, ANY domain (no synthesis). Universal join
    primitive; returns a NORMALIZED numeric `value`. Backtracks across every choice point."""
    _ctx, _hits, hit, _tried, data, _state = _search(question)
    # list-returning sources (NSF/NIH/USAspending awards) carry their number as the engine-computed
    # total, not a scalar `value` — without this a comparison over those sources finds nothing to compare
    val = data.get("value", data.get("value_usd", data.get("total_usd")))
    try:
        val = float(val)
    except (TypeError, ValueError):
        pass
    return {"source": hit["title"], "value": val, "data": data}


def run(question):
    # PLAN BEFORE FETCH. The shape of the question and the DECLARED capability of the candidate
    # sources decide whether this is one call, several, or impossible — and an impossible question
    # is refused here, without issuing a single request.
    ctx, hits = discover(question)
    if not hits:
        raise SystemExit("agent finder returned no sources")
    shape = ctx.get("shape") if ctx.get("shape") in planner.SHAPES else "point"
    # A genuinely ambiguous measure over a single entity gets SEPARATE answers per interpretation
    # (earnings -> net income, EBITDA, EPS…) instead of a silently-chosen one.
    if len(ctx.get("interpretations") or []) >= 2 and shape in ("point", "status", "entity-list"):
        data = _run_ambiguous(question, ctx)
        return {"question": question, "answer": TK.synthesize(question, data), "shape": shape,
                "plan": f"ambiguous measure → {len(data['interpretations'])} interpretations answered separately",
                "source": {"identifier": hits[0]["identifier"], "title": hits[0]["title"],
                           "publisher": hits[0].get("publisher")},
                "candidates": [{"title": h["title"], "score": h["score"], "publisher": h.get("publisher")}
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
        return {"question": question, "answer": TK.synthesize(question, data), "shape": shape,
                "plan": planner.describe(shape, p),
                "source": {"identifier": hit["identifier"], "title": hit["title"], "publisher": hit.get("publisher")},
                "candidates": [{"title": h["title"], "score": h["score"], "publisher": h.get("publisher")} for h in hits],
                "data": data}

    if p["verdict"] == "infeasible":
        need = ("a source that can see a whole population" if shape in ("ranking", "aggregate", "filtered-subset")
                else "a capability none of the matching sources declare")
        raise SystemExit(f"this is a '{shape}' question, which needs {need}; {p['why']}.")

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
    return {
        "question": question,
        "answer": TK.synthesize(question, data),
        "shape": shape,
        "plan": planner.describe(shape, p),
        "source": {"identifier": hit["identifier"], "title": hit["title"], "publisher": hit.get("publisher")},
        "candidates": [{"title": h["title"], "score": h["score"], "publisher": h.get("publisher")} for h in hits],
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
 .recs{margin-top:14px} .recs-h{font-size:.85rem;color:#888;margin:0 0 6px}
 .rec{padding:11px 14px;margin:8px 0;border:1px solid #e6e6e6;border-radius:10px;background:#fafbfc}
 .rec-t{font-weight:600;color:#1a3050;margin-bottom:5px}
 .rec-f{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:.85rem;color:#555} .rec-f b{color:#222;font-weight:600}
 .amt{color:#137333;font-weight:700}
</style></head><body>
<h1>Agentic Data Query</h1>
<p class="sub">Ask a question in plain English. An ARD Agent Finder discovers which dataset answers it; the data is fetched live, the answer is checked, and the search backtracks until it actually answers your question. <a href="/techsoup" style="color:#1a73e8">TechSoup view ›</a></p>
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
       +'<div class="covers">'+esc(s.covers)+'</div>'+(cats?'<div class="cats">'+cats+'</div>':'')+'</div>';
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
 fetch('/sources').then(function(r){return r.json()}).then(function(d){
   SRCS=d.sources||[];renderTabs(d.tabs);
 });
 f.onsubmit=function(e){e.preventDefault();var question=q.value.trim();if(!question)return;
   b.disabled=true;
   out.innerHTML='<div class="log" id="log"></div>';
   var log=document.getElementById('log');
   var cursor=document.createElement('div');cursor.className='ln';cursor.innerHTML='<span class="cur">▋</span>';log.appendChild(cursor);
   function push(html){var d=document.createElement('div');d.className='ln';d.innerHTML=html;log.insertBefore(d,cursor);log.scrollTop=log.scrollHeight;return d;}
   function status(icon,txt,cls){return push('<span class="ic">'+icon+'</span><span class="txt '+(cls||'')+'">'+txt+'</span>');}
   function fin(){if(cursor)cursor.parentNode&&cursor.remove();b.disabled=false;}
   fetch('/ask_stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:question})})
    .then(function(resp){
      var reader=resp.body.getReader(),dec=new TextDecoder(),buf='';
      function pump(){return reader.read().then(function(res){
        if(res.done){fin();return;}
        buf+=dec.decode(res.value,{stream:true});
        var parts=buf.split('\n\n');buf=parts.pop();
        parts.forEach(function(p){p=p.replace(/^data: /,'').trim();if(!p)return;var ev;try{ev=JSON.parse(p)}catch(_){return;}handle(ev);});
        return pump();
      });}
      return pump();
    }).catch(function(err){status('⚠️',esc(String(err)),'back');fin();});
   function handle(ev){
     if(ev.kind==='status'){status(ev.icon||'•',esc(ev.msg),ev.icon==='↩️'?'back':'');}
     else if(ev.kind==='plan'){
       var t='';if(ev.entity)t+='Entity <b>'+esc(ev.entity)+'</b> · ';
       t+='Metric <b>'+esc(ev.attribute||'—')+'</b> · Period '+esc(ev.period);
       t+='<div class="scan">Scanning '+ev.sources.length+' source'+(ev.sources.length==1?'':'s')+': '+esc(ev.sources.join(', '))+'</div>';
       status('\u{1F9ED}',t,'plan');
     }
     else if(ev.kind==='candidates'){
       status('\u{1F4DA}','Agent Finder ranked '+ev.items.length+' candidate table'+(ev.items.length==1?'':'s')+':');
       var mx=Math.max.apply(null,ev.items.map(function(c){return c.score||0}).concat([1]));
       ev.items.forEach(function(c,i){var w=Math.round(6+((c.score||0)/mx)*120);
         var d=document.createElement('div');d.className='cand';
         d.innerHTML='<span class="cs">'+(c.score||0)+'</span><span class="bar'+(i===0?' win':'')+'" style="width:'+w+'px"></span><span class="ct'+(i===0?' win':'')+'">'+esc(c.title)+'</span>';
         log.insertBefore(d,cursor);});
       log.scrollTop=log.scrollHeight;
     }
     else if(ev.kind==='plan_chosen'){
       var vd=ev.verdict==='infeasible'?'<span class="back">INFEASIBLE</span>'
              :(ev.verdict==='exact'?'one direct query':esc(ev.verdict.replace('compose:','')+' (several queries)'));
       status('\u{1F9ED}','Shape <b>'+esc(ev.shape)+'</b> → '+vd+(ev.why?' — '+esc(ev.why):''),'shape');
     }
     else if(ev.kind==='resolve'){
       var keys=Object.keys(ev.keys||{}).map(function(k){return '<span class="keyk">'+esc(k)+'</span> '+esc(String(ev.keys[k]))}).join(' · ');
       status('\u{1F9E9}','Resolved “'+esc(ev.mention)+'” → <b>'+esc(ev.label)+'</b>'+(keys?'  ('+keys+')':''),'rslv');
     }
     else if(ev.kind==='answer'){renderAnswer(ev);}
     else if(ev.kind==='error'){status('⚠️',esc(ev.error||'No answer.'),'back');}
   }
   function renderAnswer(d){
     if(d.error||!d.answer){status('⚠️',esc(d.error||'No answer.'),'back');return;}
     var h='<div class="answer">'+esc(d.answer)+'</div>';
     if(d.data&&d.data.ambiguous&&Array.isArray(d.data.interpretations))h+=renderInterp(d.data.interpretations);
     if(d.data&&d.data.match==='name'&&d.data.matched_entities>1)h+=renderScope(d.data);
     if(d.data&&Array.isArray(d.data.ranking)&&d.data.ranking.length)h+=renderRank(d.data.ranking,'');
     if(d.data&&Array.isArray(d.data.series)&&d.data.series.length)h+=renderRank(
        d.data.series.filter(function(s){return s.value!=null}).map(function(s){
          return {label:s.label,value:s.value}}),' ');
     if(d.data&&Array.isArray(d.data.results)&&d.data.results.length)h+=renderRecords(d.data.results);
     if(d.source)h+='<div class="src">\u{1F4DA} '+esc(d.source.title)+' <span class="pub">['+esc(d.source.publisher||'')+']</span></div>';
     if(d.candidates){h+='<details><summary>Agent Finder candidates</summary><ul>';
       d.candidates.forEach(function(c){h+='<li>'+c.score+' — '+esc(c.title)+'</li>'});h+='</ul></details>';}
     var box=document.createElement('div');box.style.marginTop='16px';box.innerHTML=h;log.parentNode.appendChild(box);
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
       return '<tr><td>'+esc(a.interpretation)+'</td><td class="v">'+v+'</td></tr>';}).join('');
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
             '<a href="/techsoup" style="color:#1a73e8">TechSoup view ›</a></p>',
             '<p class="sub">A curated view for TechSoup and the nonprofits, libraries, and '
             'foundations it serves — validate an organization, measure the digital divide, read a '
             "nonprofit's finances, understand the communities it serves, and find funding. Ask in "
             'plain English; the answer is fetched live and cited. '
             '<a href="/" style="color:#1a73e8">‹ full data explorer</a></p>')
    .replace("fetch('/sources')", "fetch('/techsoup-sources')")
    .replace('placeholder="e.g. Is the American Red Cross a 501(c)(3)?"',
             'placeholder="e.g. Is Feeding America in good standing with the IRS?"')
    .replace("<h2 class=\"sh\">Example questions</h2>\n"
             "<p class=\"shsub\">Pick a theme, then click a question to run it live.</p>",
             "<h2 class=\"sh\">What can I ask?</h2>\n"
             "<p class=\"shsub\">Grouped by what a nonprofit or its funders need. Click any question to run it live.</p>")
    .replace('<h2 class="sh">Data sources</h2>',
             '<h2 class="sh">Sources behind this view</h2>'))


def serve(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer

    class H(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
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
            p = self.path.rstrip("/")
            if p in ("", "/"):
                return self._html(PAGE)
            if p == "/techsoup":
                return self._html(TECHSOUP_PAGE)
            if p == "/sources":
                return self._json(200, {"sources": _sources_catalog(), "tabs": EXAMPLE_TABS})
            if p == "/techsoup-sources":
                # only the sources this curated view uses, plus the TechSoup-organized tabs
                dirs = {d for t in TECHSOUP_TABS for d in t["dirs"]}
                srcs = [s for s in _sources_catalog() if s["dir"] in dirs]
                return self._json(200, {"sources": srcs, "tabs": TECHSOUP_TABS})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.rstrip("/")
            if path not in ("/ask", "/ask_stream"):
                return self._json(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length", 0))
            q = json.loads(self.rfile.read(n) or b"{}").get("question", "")
            if not q:
                return self._json(400, {"error": "missing 'question'"})
            if path == "/ask":
                try:
                    self._json(200, run(q))
                except SystemExit as e:
                    self._json(200, {"question": q, "answer": None, "error": str(e)})
                return
            # Streaming play-by-play: emit each stage of the plan→find→resolve→fetch→check loop live.
            global _EMIT
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(ev):
                try:
                    self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
                    self.wfile.flush()
                    if ev.get("kind") in ("status", "resolve"):
                        time.sleep(0.55)          # let each beat land — deliberate drama
                    elif ev.get("kind") in ("plan", "candidates"):
                        time.sleep(0.7)
                except Exception:
                    pass

            _EMIT = emit
            try:
                emit({"kind": "answer", **run(q)})
            except SystemExit as e:
                emit({"kind": "error", "question": q, "error": str(e)})
            except Exception as e:
                emit({"kind": "error", "question": q, "error": str(e)})
            finally:
                _EMIT = None
                try:
                    self.wfile.write(b"data: {\"kind\":\"done\"}\n\n")
                    self.wfile.flush()
                except Exception:
                    pass

        def log_message(self, *a):
            pass

    print(f"Query harness on http://127.0.0.1:{port}/  (POST /ask)")
    HTTPServer(("127.0.0.1", port), H).serve_forever()


def main(argv):
    if not os.getenv("AZURE_OPENAI_API_KEY"):
        sys.exit("Azure keys not set: set -a; source /Users/rvguha/code/test/AskAgent/set_keys.sh; set +a")
    if argv and argv[0] == "--serve":
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8099
        return serve(port)
    print(json.dumps(run(" ".join(argv) or "How much did Apple spend on R&D in 2023?"), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
