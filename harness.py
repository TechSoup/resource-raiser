#!/usr/bin/env python3
"""Query harness — the orchestrator a skill delegates to.

  question
    -> ARD Agent Finder (POST /search via ard_client)  : which data source/table?
    -> retrieve from that source (live)                : SEC concept, or any OKF source
    -> synthesize a cited answer

Run as a CLI; the ASGI application in app.py owns HTTP serving:
  set -a; source ./set_keys.sh; set +a
  python3 harness.py "How much did Apple spend on R&D in 2023?"     # one-shot (prints JSON)
  python3 -m uvicorn app:app --host 127.0.0.1 --port 8099
"""
import asyncio, os, sys, json, math, re
import runtime
import driver, ard_client, planner, store, llm, nlweb, connectors, runtime, docpage
from domain import Attempt, Clarification, ClarificationOption, Evidence, QueryIntent
from core import Toolkit
from query_context import QueryContext

ROOT = os.path.dirname(os.path.abspath(__file__))
TK = Toolkit()

import glob as _glob


def _source_types():
    """Source name -> entityType, in a STABLE order.

    glob returns directory order, which differs between filesystems. The classifier is shown
    this list and picks the first plausible source, so an unsorted order made the same question
    route differently on macOS and on the Linux VM: "which nonprofit has the highest revenue"
    chose irs-990-bq locally and nonprofit-990 in production, which then correctly refused a
    ranking it cannot do. Deterministic per machine, divergent across them - which also means
    local results were never evidence about production for any close routing decision.
    """
    out = {}
    for p in sorted(_glob.glob(os.path.join(ROOT, "sources", "*", "_access.md"))):
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
        "How much NIH research funding does Stanford get?",
        "NSF research awards for MIT",
        "How much NIH funding does Johns Hopkins receive?",
        "Harvard University total revenue",
        "NSF research awards for Caltech",
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


def _discovery_system(src_list):
    return ("A demographic or population restriction ('for Asian residents', 'for women', 'among "
            "adults 18-64', 'for renters') is part of the ATTRIBUTE, never part of the entity. The "
            "entity is only the named company, nonprofit, place or organization: in 'unemployment "
            "rate for Asian residents in Texas' the entity is 'Texas' and the attribute is "
            "'unemployment rate for Asian residents'. Putting the restriction in the entity loses it "
            "- retrieval runs on the attribute, so the general measure is returned instead.\n"
            "Analyze a data question. Return JSON with: 'entity' (the single company/nonprofit/place/org "
            "it is about, or empty), 'entities' (ALL named entities if it compares several, else []), "
            "'type' (a short lowercase noun phrase naming the KIND of thing the entity is, using the most specific term that fits - e.g. 'company', 'educational organization', 'government agency', 'nonprofit', 'person', 'place'; 'none' when the question names no entity at all), 'attribute' (the metric/measure asked, "
            "'canonical_entity' (the entity's full commonly-used name, specific enough to identify it "
            "uniquely, disambiguated using THE QUESTION'S CONTEXT: 'St. Jude' in a question about NIH "
            "research funding is 'St. Jude Children's Research Hospital'; 'Stanford' is 'Stanford "
            "University'; a US city takes its state, 'Chicago, Illinois'), "
            "'entity_status' ('resolved' when you are confident which real-world entity is meant; "
            "'ambiguous' when the question genuinely does not distinguish it - an unqualified "
            "Springfield, Portland, Cambridge, Washington or Georgia; 'none' when the question names "
            "no entity at all, such as an exchange rate, a national total or a topical search), "
            "'entity_candidates' (when and only when the status is 'ambiguous', the 2-5 real-world "
            "entities the question could plausibly mean, each a full name in the same form as "
            "canonical_entity, most likely first - e.g. for an unqualified Springfield: "
            "['Springfield, Illinois', 'Springfield, Massachusetts', 'Springfield, Missouri']. "
            "Leave canonical_entity empty when the status is 'ambiguous' or 'none'; do not guess "
            "one, and do not invent candidates for a resolved or absent entity), "

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
            "SOURCES:\n" + src_list)




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


MAX_SEARCH_ATTEMPTS = 40




# Per-process memo caches for values that are IDENTICAL across every backtrack attempt of one
# question — the ticker for a mention, and the resolved entity candidates. Without these the harness
# re-runs the same LLM + Wikidata calls dozens of times while exhausting candidates (the capex case).
_TICKER_CACHE = {}
_ENTITY_CACHE = {}




def _entity_selection_system(name, kind, question, listing):
    return (
        f"A question mentions the {kind or 'entity'} \"{name}\". Below are database records "
        "with similar names. Return the indices of the records that ARE that entity, judging "
        "by the description - a place, a university, an album and a hospital can share a "
        "name. Usually exactly ONE record is the entity. Return an EMPTY list if none is. "
        "Return several ONLY when two records are genuinely competing readings of the same "
        "name and a person would have to choose between them - Springfield, Illinois versus "
        "Springfield, Massachusetts. A part, subsidiary, department or campus of the entity "
        "is NOT the entity: 'Stanford University School of Medicine' is not Stanford "
        "University. Neither is an article, event or topic about it: 'history of Apple Inc.' "
        "and 'Apple media event' are not Apple Inc. Judge IDENTITY ONLY: ignore which record "
        "carries useful identifiers and whether data exists for it. A record is not the "
        "intended entity merely because it has an EIN, CIK or FIPS code. "
        'Return JSON {"indices": [<n>, ...]}.\n\n'
        f"QUESTION: {question}\n\nRECORDS:\n{listing}"
    )






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


def _np_org(fetch):
    """Use an authoritative EIN when available, otherwise the question's nonprofit name."""
    organization = fetch.key or fetch.mention
    if not organization:
        raise Backtrack("no nonprofit key")
    return organization


















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


def _bind_param(binding, f):
    """Resolve the non-geographic descriptor parameter bindings shared by async fetches."""
    if binding == "$key":
        if not f.key:
            raise Backtrack("no key")
        return f.key
    if isinstance(binding, str) and binding.startswith("~"):
        return f.fm.get(binding[1:], "")
    return binding




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




_ADJUDICATION_SYSTEM = (
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
    'euro was asked). When in doubt, ACCEPT. Return JSON {"ok": true|false, "why": "<short reason>"}.'
)




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












def _entity_clarification(question, ctx, candidates, ledger, discovery):
    """Ask which entity was meant, before spending a fetch on any of them.

    The measure clarification fetches every interpretation first, because two names for the
    same measure can turn out to be the same number and not worth interrupting anyone over.
    Entities are not like that: Springfield, Illinois and Springfield, Missouri both return a
    valid population, and the difference is the whole question.

    Nothing is looked up here. These are the names the classifier produced, and no identifier
    registry can tell us which one the caller meant.
    """
    # Candidates are either names the classifier produced or records the crosswalk matched.
    options = []
    for c in candidates:
        if isinstance(c, dict):
            label = c.get("name") or c.get("label") or ""
            desc = c.get("description") or ""
            options.append(ClarificationOption(id=c.get("qid") or label,
                                               label=f"{label} — {desc}" if desc else label,
                                               assumptions={"entity": label,
                                                            "entity_qid": c.get("qid") or ""}))
        else:
            options.append(ClarificationOption(id=c, label=c, assumptions={"entity": c}))
    # The classifier leaves `entity` empty when it declines to resolve, so name the thing the
    # caller actually typed by taking what the candidates share.
    subject = (ctx.get("entity") or "").strip() or (options[0].label.split(",")[0] if options else "That name")
    clar = Clarification(question=f"“{subject}” could mean more than one place or "
                                  f"organization. Which one do you mean?",
                         options=options,
                         attribute=ctx.get("attribute") or "the requested measure")
    return {"question": question, "status": "needs_clarification", "answer": None,
            "answer_renderer": None, "clarification": clar.to_dict(),
            "shape": ctx.get("shape") or "point", "usage": ledger.snapshot(),
            "discovery_usage": discovery.snapshot(),
            "data": {"ambiguous_entity": True, "candidates": [o.label for o in options]},
            "plan": f"ambiguous entity → ask which of {len(options)} the caller means"}


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


async def _ambiguity_result(question, ctx, hits, intent, clarification, on_ambiguity,
                            ledger, discovery, attempts=None, *, context):
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
        answer, renderer = await _present_async(question, evidence, context=context)
        return {**base, "status": "answered", "answer_renderer": renderer,
                "answer": answer, "usage": ledger.snapshot(),
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
    answer, renderer = await _present_async(question, point, context=context)
    payload["selected"] = selected.id
    return {**base, "status": "answered", "answer": answer,
            "usage": ledger.snapshot(), "answer_renderer": renderer,
            "evidence": point.to_dict(),
            "plan": f"material ambiguity → answer the preferred interpretation and expose {len(public_options) - 1} alternatives"}












# --- event-loop-native query engine ---------------------------------------------------------------

async def _asay(context, kind, **data):
    await context.emit(kind, **data)


async def discover_async(question, sites=None, assumptions=None, *, context):
    """Classify and discover without crossing a synchronous provider boundary."""
    await _asay(context, "status", icon="🔍", msg="Reading your question…")
    src_list = "\n".join(f"- {directory}: covers {entity_type}"
                          for directory, entity_type in SOURCE_TYPES.items())
    system = _discovery_system(src_list)
    try:
        classified = await llm.chat_async(
            system, question, context=context, json_mode=True, stage="classify")
        ctx = _normalize_shape(json.loads(classified))
    except (ValueError, TypeError) as exc:
        raise runtime.Refused(f"question classification returned invalid JSON: {exc}") from exc
    ctx["question"] = question
    if isinstance(assumptions, dict):
        allowed = {"entity", "type", "attribute", "period", "shape", "concept", "entity_qid"}
        applied = {key: value for key, value in assumptions.items()
                   if key in allowed and value not in (None, "")}
        ctx.update(applied)
        if "attribute" in applied:
            ctx["interpretations"] = []
        if applied.get("entity"):
            ctx.update(entity_status="resolved", canonical_entity=applied["entity"],
                       entity_candidates=[])
        ctx = _normalize_shape(ctx)
    if not (ctx.get("entity") or "").strip():
        recovered = _recover_place(question)
        if recovered:
            ctx["entity"] = recovered
    await _asay(context, "entity_detected", entity=ctx.get("entity") or "",
                canonical=ctx.get("canonical_entity") or "", type=ctx.get("type") or "none",
                status=ctx.get("entity_status") or "none")
    await _asay(context, "property_identified", attribute=ctx.get("attribute") or "",
                interpretations=ctx.get("interpretations") or [],
                period=ctx.get("period") or "latest", shape=ctx.get("shape") or "point")
    sources = [source for source in (ctx.get("sources") or []) if source in SOURCE_TYPES]
    sources = _ensure_grant_graph(question, sources or list(SOURCE_TYPES))
    if sites:
        wanted = [source for source in sites if source in SOURCE_TYPES]
        if wanted:
            sources = wanted
    if (ctx.get("shape") in ("point", "status", "entity-list", "comparison", "timeseries")
            and ctx.get("entity") and all(source.endswith("-bq") for source in sources)):
        sources = list(SOURCE_TYPES)
    await _asay(context, "plan", entity=ctx.get("entity") or "", type=ctx.get("type") or "none",
                attribute=ctx.get("attribute") or "", period=ctx.get("period") or "latest",
                shape=ctx.get("shape") or "point", sources=sources)
    # Do NOT gate on a fixed type vocabulary: the classifier returns an open noun phrase.
    # What this actually asks is whether a specific named entity was identified.
    resolvable = bool((ctx.get("entity") or "").strip()) and \
        (ctx.get("entity_status") or "").strip().lower() != "none"
    attribute = ctx.get("attribute") or ""
    readings = [reading for reading in (ctx.get("interpretations") or []) if reading]
    if not attribute and readings:
        attribute = readings[0]
    primary = (attribute or question) if resolvable else question
    secondary = question if resolvable else (attribute or question)
    extra = readings[1:3] if not ctx.get("attribute") and readings else []
    await _asay(context, "status", icon="📚",
                msg="Asking the ARD Agent Finder which data tables can answer this…")
    try:
        found = await ard_client.search_many_async(
            [primary, secondary] + extra, k=12, sources=sources,
            rerank_query=question, context=context)
    except ard_client.DiscoveryError as exc:
        raise runtime.Refused(str(exc)) from exc
    seen, hits = set(), []
    for hit in found:
        if hit["identifier"] not in seen:
            seen.add(hit["identifier"])
            hits.append(hit)
    await _asay(context, "candidates", count=len(hits), items=[
        {"title": hit["title"], "score": hit["score"], "publisher": hit.get("publisher")}
        for hit in hits[:6]])
    return ctx, hits


async def _solve_async(steps, goal, state, i=0):
    """Sequential depth-first async solver with the synchronous solver's prune semantics."""
    if i == len(steps):
        return await goal(state)
    name, options_fn = steps[i]
    options = options_fn(state)
    if hasattr(options, "__await__"):
        options = await options
    last = None
    for option in options:
        try:
            return await _solve_async(steps, goal, {**state, name: option}, i + 1)
        except Prune as exc:
            if exc.step != name:
                raise
            last = exc
        except Backtrack as exc:
            last = exc
    raise Backtrack(f"no viable {name} ({last})")


async def _link_records_async(name, question="", kind="", *, context):
    name = (name or "").strip()
    if not name:
        return []
    cache = context.memo.setdefault("entities", {})
    key = (name, question)
    if key in cache:
        return cache[key]
    import resolver
    try:
        candidates = await resolver.search_async(name, context=context)
    except (asyncio.CancelledError, runtime.QueryCancelled):
        raise
    except Exception:
        candidates = []
    await _asay(context, "entity_mapping", phase="candidates", mention=name,
                count=len(candidates))
    if not candidates:
        cache[key] = []
        return []
    listing = "\n".join(f"{index}. {candidate.get('label', '')} — {candidate.get('description', '')}"
                         for index, candidate in enumerate(candidates))
    try:
        raw = await llm.chat_async(
            _entity_selection_system(name, kind, question, listing),
            name, context=context, json_mode=True, stage="resolve-entity")
        indices = json.loads(raw).get("indices")
    except (asyncio.CancelledError, runtime.QueryCancelled):
        raise
    except Exception:
        indices = None
    if not isinstance(indices, list):
        cache[key] = []
        return []
    found = []
    for index in indices:
        if not isinstance(index, int) or not 0 <= index < len(candidates):
            continue
        try:
            qid = candidates[index]["id"]
            label, keys = await resolver.claims_async(qid, context=context)
        except (asyncio.CancelledError, runtime.QueryCancelled):
            raise
        except Exception:
            continue
        found.append({"qid": qid, "label": label or candidates[index].get("label") or name,
                      "name": candidates[index].get("label") or name,
                      "description": candidates[index].get("description") or "", "keys": keys})
    cache[key] = found
    return found


async def _link_entity_async(ctx, *, context):
    status = (ctx.get("entity_status") or "").strip().lower()
    canonical = (ctx.get("canonical_entity") or "").strip()
    mention = (ctx.get("entity") or "").strip()
    cache = context.memo.setdefault("linked_entities", {})
    cache_key = (status, canonical, mention, (ctx.get("entity_qid") or "").strip(),
                 ctx.get("question") or "", ctx.get("type") or "")
    if cache_key in cache:
        return cache[cache_key]

    async def finish(result, phase, **data):
        cache[cache_key] = result
        await _asay(context, "entity_mapping", phase=phase, mention=mention,
                    canonical=canonical, **data)
        return result

    if status in ("none", "ambiguous"):
        reason = "no named entity" if status == "none" else "entity needs clarification"
        return await finish([None], "skipped", reason=reason)
    import resolver
    qid = (ctx.get("entity_qid") or "").strip()
    if not (canonical or mention or qid):
        return await finish([None], "skipped", reason="no named entity")
    await _asay(context, "entity_mapping", phase="searching", mention=mention,
                canonical=canonical, qid=qid)
    if qid:
        try:
            label, keys = await resolver.claims_async(qid, context=context)
        except (asyncio.CancelledError, runtime.QueryCancelled):
            raise
        except Exception:
            label, keys = None, {}
        if label or keys:
            entity = {"qid": qid, "label": label or canonical or qid,
                      "name": canonical or label or qid, "keys": keys}
            return await finish([entity, None], "mapped", label=entity["label"], qid=qid,
                                key_types=sorted(keys))
    found = await _link_records_async(canonical or mention, ctx.get("question") or "",
                                      ctx.get("type") or "", context=context)
    if len(found) > 1:
        return await finish(found, "ambiguous", count=len(found),
                            labels=[entity.get("label") for entity in found])
    if found:
        entity = found[0]
        return await finish([entity, None], "mapped", label=entity.get("label") or mention,
                            qid=entity.get("qid") or "",
                            key_types=sorted((entity.get("keys") or {}).keys()))
    return await finish([None], "not_found", reason="no matching crosswalk record")


async def _place_levels_async(entity, *, context):
    if not entity:
        return []
    import resolver
    try:
        return await resolver.hierarchy_async(entity["qid"], context=context)
    except (asyncio.CancelledError, runtime.QueryCancelled):
        raise
    except Exception:
        return [{"label": entity.get("label"), "keys": entity.get("keys", {})}]


async def _resolve_geo_async(place, *, context):
    fips = _STATE_FIPS.get((place or "").strip().lower().lstrip("the ").strip())
    if fips:
        return f"state:{fips}"
    raw = await llm.chat_async(
        "Convert this US place to a Census geography clause. A state is state:NN; a county is "
        'county:CCC&in=state:NN; a city is place:PPPPP&in=state:NN. JSON {"geo":"..."}.',
        place, context=context, json_mode=True, stage="resolve-entity")
    return json.loads(raw).get("geo")


async def _key_options_async(state, ctx, *, context):
    fm = driver.frontmatter(state["hit"]["identifier"])
    keys = (state["entity"] or {}).get("keys", {})
    mention = ctx.get("entity") or ""
    if fm.get("concept"):
        return ([str(int(keys["cik"]))] if keys.get("cik") else []) + [None]
    if fm.get("field") or fm.get("classification") or fm.get("bmf"):
        return (([str(keys["ein"]).replace("-", "")] if keys.get("ein") else [])
                + ([mention] if mention else []) or [None])
    if fm.get("profile"):
        return ([(state["entity"] or {}).get("qid")] if (state["entity"] or {}).get("qid") else []) or [None]
    if fm.get("scorecard"):
        return [mention] if mention else [None]
    levels = await _place_levels_async(state.get("entity"), context=context)
    if fm.get("fema"):
        states = [level["keys"].get("fips_state") for level in levels
                  if level["keys"].get("fips_state")]
        return states + ([mention] if mention else []) or [None]
    if fm.get("variable"):
        geos = [geo for geo in (_geo_from_fips(level["keys"]) for level in levels) if geo]
        return geos + (["__native__"] if mention else []) or [None]
    if fm.get("measureid"):
        labels = [level["label"].replace(" County", "").strip() for level in levels
                  if level.get("label")]
        return labels or ([mention.replace(" County", "").strip()] if mention else [None])
    if fm.get("search", {}).get("want") == "organization":
        label = (state["entity"] or {}).get("label")
        return list(dict.fromkeys(value for value in (label, mention) if value)) or [None]
    return [None]


async def _s_concept_async(f, *, context):
    if f.key:
        return await driver.fetch_metric_async(
            f.attribute, cik=f.key, period=f.period, log=False,
            concept=f.ctx.get("concept"), context=context)
    tickers = context.memo.setdefault("tickers", {})
    if f.mention not in tickers:
        raw = await llm.chat_async(
            'JSON {"ticker":"<US stock ticker or empty>"}.', f.mention,
            context=context, json_mode=True, stage="resolve-entity")
        tickers[f.mention] = json.loads(raw).get("ticker")
    if not tickers[f.mention]:
        raise Backtrack("no ticker")
    return await driver.fetch_metric_async(
        f.attribute, tickers[f.mention], f.period, log=False,
        concept=f.ctx.get("concept"), context=context)


async def _quirk_acs_pe_async(f, response, record, *, context):
    if not (isinstance(response, list) and len(response) >= 2):
        raise Backtrack("no census row")
    def jam(value):
        try:
            return float(value) <= -100000000
        except (TypeError, ValueError):
            return False
    value, variable = record.get("value"), record.get("variable") or ""
    if str(value).strip() == "-888888888" and variable.endswith("E") and not variable.endswith("PE"):
        percent = variable[:-1] + "PE"
        geo = await _resolve_geo_async(f.mention, context=context) if f.key == "__native__" else f.key
        sibling = await driver.accessor_async(f.ident, "acs", geo=geo, get=percent, context=context)
        if isinstance(sibling, list) and len(sibling) >= 2 and not jam(sibling[1][1]):
            value, variable = sibling[1][1], percent
    if jam(value):
        raise Backtrack("jam null")
    record["value"], record["variable"] = value, variable
    return record


async def _bind_param_async(binding, f, *, context):
    if binding == "$geo":
        geo = await _resolve_geo_async(f.mention, context=context) if f.key == "__native__" else f.key
        if not geo:
            raise Backtrack("no geo")
        return geo
    return _bind_param(binding, f)


async def _s_rest_async(f, *, context):
    spec = _fetch_spec(f)
    if not spec:
        raise Backtrack("no fetch spec for this source")
    params = {key: await _bind_param_async(value, f, context=context)
              for key, value in (spec.get("params") or {}).items()}
    if spec.get("query"):
        query = re.sub(r"~(\w+)", lambda match: str(f.fm.get(match.group(1), "")), spec["query"])
        filter_field = spec.get("filter_field")
        if filter_field and f.fm.get(filter_field):
            query += f"&filter={f.fm[filter_field]}"
        params["query"] = query
    response = await driver.accessor_async(
        f.ident, spec.get("op", "get"), context=context, **params)
    rows = _rows_of_resp(response, spec.get("rows"))
    row = _pick_row(rows, spec["pick"]) if spec.get("pick") else None
    if spec.get("pick") and row is None:
        raise Backtrack("no matching row")
    record = {key: value for key, binding in (spec.get("fields") or {}).items()
              if (value := _bind_field(binding, f, response, row)) is not None}
    if spec.get("quirk") == "acs_pe":
        record = await _quirk_acs_pe_async(f, response, record, context=context)
    record["source"] = spec.get("source")
    return record


async def _s_search_async(f, *, context):
    search = f.fm["search"]
    value = (f.key or f.mention) if search["want"] == "organization" else f.attribute
    if not value:
        raise Backtrack("no search term")
    capability = (planner.capabilities(f.ident) or {}).get(search["operation"], {})
    page = capability.get("page") or {}
    async def pull(**extra):
        result = await driver.accessor_async(
            f.ident, search["operation"], context=context, **{search["arg"]: value, **extra})
        for part in search["extract"].split("."):
            result = result[int(part)] if isinstance(result, list) else result.get(part, [])
        return result if isinstance(result, list) else []
    if page.get("complete_for") == "entity" and page.get("offset_param"):
        step, offset, results = int(page.get("max") or 500), 0, []
        while offset < int((capability.get("population") or {}).get("ceiling") or 15000):
            chunk = await pull(**{page["offset_param"]: offset})
            results.extend(chunk)
            if len(chunk) < step:
                break
            offset += step
    else:
        results = await pull()
    out = {"query": value, "source": f.fm.get("title")}
    rows = [row for row in results if isinstance(row, dict)]
    total = sum(_amount(row) for row in rows)
    out.update(record_count=len(rows), complete=bool(page.get("complete"))
               or page.get("complete_for") == "entity")
    if total:
        out.update(total_usd=round(total, 2), total_usd_display="${:,.0f}".format(total))
    if not out["complete"]:
        out["coverage"] = (f"total is across the {len(rows)} award records returned by this "
                           "query, not every award the organization has received")
    out.update(_identity_scope(rows, f.fm.get("identity") or {}))
    out["results"] = [{key: value for key, value in row.items()
                       if not (isinstance(value, str) and len(value) > 240)} for row in rows][:8]
    return out


async def _fetch_async(state, ctx, *, context):
    identifier = state["hit"]["identifier"]
    fm = driver.frontmatter(identifier)
    f = _F(fm, identifier, state.get("key"), state.get("period") or "latest",
           ctx.get("attribute") or "", ctx.get("entity") or "", state, ctx)
    try:
        if fm.get("concept"):
            return await _s_concept_async(f, context=context)
        import nonprofit
        if fm.get("classification"):
            return await nonprofit.classify_async(_np_org(f), context=context)
        if fm.get("field"):
            return await nonprofit.fetch_np_async(fm["field"], _np_org(f), f.period, context=context)
        if fm.get("bmf"):
            return await nonprofit.bmf_async(fm["bmf"], _np_org(f), context=context)
        if fm.get("profile"):
            if not f.key:
                raise Backtrack("no wikidata qid")
            import orgprofile
            return await orgprofile.fetch_async(
                fm["profile"], f.key, (f.state.get("entity") or {}).get("label"), context=context)
        if fm.get("scorecard"):
            import college
            return await college.fetch_async(fm["scorecard"], f.key or f.mention, context=context)
        if fm.get("fema"):
            import fema
            return await fema.fetch_async(f.key or f.mention, context=context)
        if any(fm.get(marker) for marker in ("variable", "measureid", "tfield")):
            return await _s_rest_async(f, context=context)
        if fm.get("search"):
            return await _s_search_async(f, context=context)
    except runtime.Refused as exc:
        raise Backtrack(str(exc)) from exc
    raise Backtrack("no structured retrieval for this source")


async def _answers_async(question, data, structural=None, *, context):
    if structural is not None:
        if not structural.accepted:
            return False, structural.reason
        if not structural.residual_semantic_check:
            return True, ""
    try:
        raw = await llm.chat_async(
            _ADJUDICATION_SYSTEM,
            json.dumps({"question": question, "data": data}), context=context,
            json_mode=True, stage="check")
        verdict = json.loads(raw)
        ok, why = bool(verdict.get("ok", True)), verdict.get("why", "")
        if not ok and re.search(r"\b(fy\d*|fiscal|period|years?|dates?|20\d\d|recent|latest|current)\b",
                                why or "", re.I):
            return True, ""
        return ok, why
    except runtime.QueryCancelled:
        raise
    except Exception:
        return True, ""


async def _search_async(question, ctx=None, hits=None, *, context):
    if ctx is None or hits is None:
        ctx, hits = await discover_async(question, context=context)
    if not hits:
        raise runtime.Refused("agent finder returned no sources")
    period = ctx.get("period") or "latest"
    intent, trace = QueryIntent.from_context(question, ctx), []
    context.memo["attempts"] = trace
    steps = [
        ("hit", lambda state: hits),
        ("entity", lambda state: _link_entity_async(ctx, context=context)),
        ("key", lambda state: _key_options_async(state, ctx, context=context)),
        ("period", lambda state: [period, "latest"] if period != "latest" else ["latest"]),
    ]
    attempts, tried_tables, done = 0, set(), {}

    async def goal(state):
        nonlocal attempts
        if attempts >= MAX_SEARCH_ATTEMPTS:
            raise runtime.Refused(
                f"no source could answer this. {len(tried_tables)} of {len(hits)} candidate tables "
                f"were tried in {attempts} attempts before the search budget ran out.")
        entity = (state.get("entity") or {}).get("label")
        identity = (state["hit"]["identifier"], (state.get("entity") or {}).get("qid"), entity,
                    json.dumps(state.get("key"), sort_keys=True, default=str), state.get("period"))
        if identity in done:
            raise Backtrack(f"already attempted ({done[identity]})")
        try:
            context.budget.consume_attempt()
        except runtime.QueryBudgetExceeded as exc:
            raise runtime.QueryBudgetExceeded(
                f"{exc}; this branch tried {len(tried_tables)} of {len(hits)} candidate tables "
                f"in {attempts} attempts") from exc
        attempts += 1
        tried_tables.add(state["hit"]["identifier"])
        attempt = Attempt(source=state["hit"].get("publisher") or state["hit"]["title"],
                          identifier=state["hit"]["identifier"], entity=state.get("entity"),
                          period=state.get("period") or "latest")
        connector = connectors.for_hit(state["hit"])
        try:
            evidence = await connector.execute_async(
                intent, attempt, state["hit"],
                lambda: _fetch_async(state, ctx, context=context),
                adjudicator=lambda data, verdict: _answers_async(
                    question, data, verdict, context=context))
        except connectors.Rejected as exc:
            done[identity] = "wrong table"
            trace.append(exc.attempt)
            raise Prune("hit", f"answer rejected: {exc}") from exc
        except Backtrack as exc:
            done[identity] = str(exc)
            trace.append(attempt)
            raise
        except runtime.QueryCancelled:
            trace.append(attempt)
            raise
        trace.append(attempt)
        return {**state, "_data": evidence.payload, "_evidence": evidence, "_attempts": trace}

    try:
        state = await _solve_async(steps, goal, {})
    except Backtrack as exc:
        raise runtime.Refused(f"no source could answer: {exc}") from exc
    return ctx, hits, state["hit"], hits.index(state["hit"]) + 1, state["_data"], state


async def _present_async(question, evidence, *, context):
    data = dict(evidence.payload)
    metadata = {
        "evidence_kind": evidence.kind, "source": evidence.source,
        "entity": evidence.entity, "measure": evidence.measure, "unit": evidence.unit,
        "currency": evidence.currency, "period": evidence.period,
        "warnings": evidence.warnings,
    }
    for key, value in metadata.items():
        if value not in (None, "", []):
            data.setdefault(key, value)
    answer = await TK.synthesize_async(question, data, context=context)
    return answer.strip(), "llm-synthesis"


async def retrieve_for(question, *, context):
    """Async universal join primitive; concurrent callers use forked scratch state."""
    _ctx, _hits, hit, _tried, data, state = await _search_async(question, context=context)
    val = data.get("value", data.get("value_usd", data.get("total_usd")))
    try:
        val = float(val)
    except (TypeError, ValueError):
        pass
    return {"source": hit["title"], "source_identifier": hit.get("identifier"),
            "value": val, "data": data,
            "attempts": [a.to_dict() for a in (state.get("_attempts") or [])]}


async def _ordered(context, factories):
    """Run branches concurrently and return their values in plan order."""
    context.budget.consume_fanout(len(factories))
    values = [None] * len(factories)
    async def one(index, factory):
        values[index] = await factory(context.fork())
    try:
        async with asyncio.TaskGroup() as group:
            for index, factory in enumerate(factories):
                group.create_task(one(index, factory))
    except BaseExceptionGroup as group:
        leaves, pending = [], list(group.exceptions)
        while pending:
            exc = pending.pop(0)
            if isinstance(exc, BaseExceptionGroup):
                pending[0:0] = list(exc.exceptions)
            else:
                leaves.append(exc)
        # A sibling cancellation is normally TaskGroup cleanup, not the cause. Preserve an
        # intentional refusal (including QueryBudgetExceeded subclasses), then the first real
        # failure, and use cancellation only when nothing more informative exists.
        refused = next((exc for exc in leaves if isinstance(exc, runtime.Refused)), None)
        if refused is not None:
            raise refused
        real = next((exc for exc in leaves if not isinstance(
            exc, (asyncio.CancelledError, runtime.QueryCancelled))), None)
        if real is not None:
            raise real
        if leaves:
            raise leaves[0]
        raise
    return values


async def _admit_async(intent, hit, data, *, context):
    attempt = Attempt(source=hit.get("publisher") or hit.get("title") or "",
                      identifier=hit.get("identifier") or "", period=intent.period)
    evidence = await connectors.for_hit(hit).execute_async(
        intent, attempt, hit, lambda: asyncio.sleep(0, result=data),
        adjudicator=lambda payload, verdict: _answers_async(
            intent.question, payload, verdict, context=context))
    return evidence, [attempt]


async def _run_bq_async(question, ctx, p, *, context):
    import bq
    cfg = (driver.frontmatter(p["hit"]["identifier"]) or {})["bq"]
    if ctx.get("shape") == "aggregate":
        return await bq.aggregate_async(cfg, "count", context=context)
    asc = any(word in question.lower() for word in
              ("lowest", "least", "smallest", "fewest", "bottom"))
    threshold = ctx.get("threshold") if ctx.get("shape") == "filtered-subset" else None
    return await bq.rank_async(cfg, n=10, ascending=asc, threshold=threshold, context=context)


async def _run_grants_async(question, ctx, *, context):
    import grants
    direction = _grant_direction(question, ctx, grants)
    ql = question.lower()
    asc = any(word in ql for word in ("lowest", "least", "smallest", "fewest", "bottom"))
    entity = (ctx.get("entity") or "").strip()
    if direction == "ranking":
        threshold = ctx.get("threshold") or {}
        if threshold.get("value") is not None:
            return await grants.funders_above_async(
                threshold["value"], ascending=str(threshold.get("op", ">")).startswith("<"),
                context=context)
        return await grants.top_grantmakers_async(n=10, ascending=asc, context=context)
    if direction == "biggest_recipients":
        by = "funders" if any(word in ql for word in
            ("most funders", "most foundations", "most donors", "different funders",
             "different foundations", "how many funder")) else "dollars"
        return await grants.biggest_recipients_async(n=10, by=by, ascending=asc, context=context)
    if direction == "geo":
        states = grants.find_states(question)
        if len(states) >= 2:
            return await grants.geo_async("flow", from_state=states[0], to_state=states[1],
                                          context=context)
        mode = "funders" if any(word in ql for word in
            ("send", "sent", "sending", "give the most", "gives the most", "from which state",
             "which states give")) else "recipients"
        return await grants.geo_async(mode, ascending=asc, context=context)
    if direction == "overview":
        match = re.search(r"20(2[0-4])", question)
        return await grants.overview_async(year=int(match.group(0)) if match else None,
                                           context=context)
    if direction == "theme":
        _major, word = grants.cause_of(ql)
        grouped = any(term in ql for term in
                      ("what cause", "which cause", "by cause", "kinds of", "breakdown"))
        return await grants.grants_by_cause_async(None if grouped or not word else word,
                                                  context=context)
    if direction == "shared":
        entities = [item for item in (ctx.get("entities") or []) if item] or ([entity] if entity else [])
        if len(entities) < 2:
            raise runtime.Refused("comparing shared grantees needs TWO named funders.")
        return await grants.shared_grantees_async(entities[0], entities[1], context=context)
    org = _grant_entity(question, ctx)
    if not org:
        raise runtime.Refused("this grant question needs a named organization (a funder or a recipient).")
    if direction == "reverse":
        return await grants.reverse_async(org, context=context)
    return await grants.forward_async(org, context=context)


async def _run_ranking_async(question, ctx, p, top_n=10, *, context):
    hit, cap, operation = p["hit"], p["capability"], p["operation"]
    fm = driver.frontmatter(hit["identifier"]) or {}
    access = driver.frontmatter(planner.access_path(hit["identifier"])) or {}
    operation_doc = ((access.get("access") or {}).get("operations") or {}).get(operation, {})
    needed = {field for _, field, _, _ in __import__("string").Formatter().parse(
        operation_doc.get("url", "")) if field}
    params = {key: fm[key] for key in needed if key in fm}
    threshold = ctx.get("threshold") or {}
    if "n" in needed:
        params["n"] = 500 if threshold.get("value") is not None else max(top_n, 25)
    for key in needed - set(params):
        if key in ("level", "fips", "geo"):
            params[key] = (ctx.get("partition") or {}).get(key) or ""
    rows = _rows_of(await driver.accessor_async(
        hit["identifier"], operation, context=context, **params), cap)
    if not rows:
        raise runtime.Refused(f"ranking returned no usable rows from {hit['title']}")
    if not (cap.get("order") or {}).get("server"):
        rows.sort(key=lambda row: row["value"], reverse=True)
    if any(word in question.lower() for word in ("lowest", "least", "smallest", "fewest", "bottom")):
        rows.sort(key=lambda row: row["value"])
    scanned = len(rows)
    out = {"question": question, "source": fm.get("title") or hit["title"],
           "measure": (fm.get("title") or "").split(" — ")[0], "scanned": scanned,
           "complete": True}
    if threshold.get("value") is not None:
        import operator
        compare = {">": operator.gt, ">=": operator.ge, "<": operator.lt,
                   "<=": operator.le}.get(threshold.get("op"), operator.gt)
        kept = [row for row in rows if compare(row["value"], float(threshold["value"]))]
        out.update({"threshold": f"{threshold.get('op', '>')} {threshold['value']}",
                    "matches": len(kept), "ranking": kept[:50]})
        if kept and len(kept) >= scanned:
            out["complete"] = False
            out["note"] = f"at least {len(kept)} — the {scanned}-row scan window filled up"
        elif not kept:
            out["note"] = f"no member of the population is {threshold.get('op', '>')} {threshold['value']}"
        return out
    rows = rows[:top_n]
    out.update({"ranking": rows, "top": rows[0]})
    return out


async def _run_ambiguous_async(question, ctx, *, context):
    interpretations = [item for item in (ctx.get("interpretations") or []) if item][:4]
    entity, period = ctx.get("entity") or "", ctx.get("period") or "latest"
    year = "" if period == "latest" else f" in {period}"
    async def branch(interp, branch_context):
        subquestion = f"{interp} for {entity}{year}" if entity else f"{interp}{year}"
        try:
            result = await retrieve_for(subquestion, context=branch_context)
            data = result.get("data") or {}
            return {"interpretation": interp, "value": result.get("value"),
                    "label": data.get("metric") or data.get("measure") or interp,
                    "unit": data.get("unit"), "period": data.get("period"),
                    "source": result.get("source"), "concept": data.get("concept"),
                    "source_identifier": result.get("source_identifier"),
                    "attempts": result.get("attempts") or []}
        except driver.SourceRateLimitError as exc:
            return {"interpretation": interp, "value": None, "temporary_error": str(exc)}
        except (runtime.Refused, runtime.Refused, Backtrack) as exc:
            return {"interpretation": interp, "value": None, "error": str(exc)}
    answers = await _ordered(context, [
        lambda branch_context, interp=interp: branch(interp, branch_context)
        for interp in interpretations])
    temporary = next((item["temporary_error"] for item in answers if item.get("temporary_error")), None)
    if temporary:
        raise driver.SourceRateLimitError(temporary)
    got = [item for item in answers if isinstance(item.get("value"), (int, float))]
    if not got:
        raise runtime.Refused(f"'{ctx.get('attribute')}' is ambiguous and none of its interpretations could be answered")
    return {"question": question, "ambiguous": True, "attribute": ctx.get("attribute"),
            "entity": entity, "interpretations": answers,
            "source": " · ".join(dict.fromkeys(item.get("source") or "?" for item in got))}


async def _run_fanout_async(question, ctx, shape, *, context):
    attribute = ctx.get("attribute") or ""
    if shape == "timeseries":
        years = [year for year in (ctx.get("periods") or []) if year][:20]
        if len(years) < 2:
            raise runtime.Refused("timeseries needs at least two periods")
        _c, _h, hit, _t, _d, state = await _search_async(
            f"{attribute} for {ctx.get('entity') or ''}", context=context.fork())
        async def year_branch(year, branch_context):
            try:
                data = await _fetch_async({**state, "period": str(year)}, ctx,
                                          context=branch_context)
                return {"label": str(year),
                        "value": data.get("value", data.get("value_usd", data.get("total_usd"))),
                        "source": hit["title"]}
            except (Backtrack, runtime.Refused, runtime.Refused) as exc:
                return {"label": str(year), "value": None, "error": str(exc)}
        series = await _ordered(context, [
            lambda branch_context, year=year: year_branch(year, branch_context) for year in years])
    else:
        if shape == "comparison":
            subquestions = [(entity, f"{attribute} for {entity}")
                            for entity in (ctx.get("entities") or []) if entity][:8]
        else:
            subquestions = [(year, f"{attribute} for {ctx.get('entity') or ''} in {year}")
                            for year in (ctx.get("periods") or []) if year][:20]
        if len(subquestions) < 2:
            raise runtime.Refused(f"{shape} needs at least two values")
        async def sub_branch(label, subquestion, branch_context):
            try:
                result = await retrieve_for(subquestion, context=branch_context)
                leaf = result.get("data") or {}
                return {"label": str(label), "value": result.get("value"),
                        "source": result.get("source"),
                        "period": (leaf.get("period") or leaf.get("year") or
                                   leaf.get("fiscal_year")),
                        "unit": (leaf.get("unit") or leaf.get("units") or
                                 ("USD" if "value_usd" in leaf else None)),
                        "currency": (leaf.get("currency") or
                                     ("USD" if "value_usd" in leaf else None))}
            except (runtime.Refused, runtime.Refused) as exc:
                return {"label": str(label), "value": None, "error": str(exc)}
        series = await _ordered(context, [
            lambda branch_context, label=label, subquestion=subquestion:
                sub_branch(label, subquestion, branch_context)
            for label, subquestion in subquestions])
        hit = None
    got = [item for item in series if isinstance(item.get("value"), (int, float))]
    if len(got) < 2:
        raise runtime.Refused(f"could not retrieve comparable values for {shape}")
    out = {"question": question, "shape": shape, "attribute": attribute, "series": series,
           "source": hit["title"] if hit else got[0].get("source")}
    units = {item.get("unit") for item in got if item.get("unit")}
    currencies = {item.get("currency") for item in got if item.get("currency")}
    if len(units) == 1:
        out["unit"] = units.pop()
    if len(currencies) == 1:
        out["currency"] = currencies.pop()
    if shape == "comparison":
        best = max(got, key=lambda item: item["value"])
        out.update({"highest": best["label"],
                    "difference": round(best["value"] - min(item["value"] for item in got), 2)})
        periods = {str(item.get("period")) for item in got if item.get("period")}
        if len(periods) > 1:
            out["alignment_warnings"] = ["the compared figures cover different reporting periods"]
    else:
        first, last = got[0], got[-1]
        out["change"] = round(last["value"] - first["value"], 2)
        if first["value"]:
            out["change_pct"] = round((last["value"] - first["value"]) / abs(first["value"]) * 100, 1)
    return out


async def _run_derive_async(question, ctx, *, context):
    spec = json.loads(await llm.chat_async(
        "Decompose this into the INDEPENDENT figures needed, each a self-contained sub-question "
        "naming its entity and measure explicitly. Return JSON "
        "{\"parts\":[{\"label\":\"<short name>\",\"question\":\"<sub-question>\"}],"
        "\"compute\":\"share|ratio|difference|sum\",\"of\":\"<numerator/left label>\","
        "\"per\":\"<denominator/right label>\"}.",
        question, context=context, json_mode=True, stage="classify"))
    parts = [part for part in (spec.get("parts") or []) if part.get("question")][:4]
    if len(parts) < 2:
        raise runtime.Refused("could not decompose this into two or more figures to join")
    async def branch(part, branch_context):
        try:
            result = await retrieve_for(part["question"], context=branch_context)
            data = result.get("data") or {}
            return {"label": part["label"], "question": part["question"],
                    "value": result.get("value"), "source": result.get("source"),
                    "period": data.get("period") or data.get("as_of") or data.get("fiscal_year"),
                    "complete": data.get("complete", True),
                    "matched_entities": data.get("matched_entities"),
                    "coverage": data.get("coverage")}
        except (runtime.Refused, runtime.Refused) as exc:
            return {"label": part["label"], "value": None, "error": str(exc)}
    values = await _ordered(context, [
        lambda branch_context, part=part: branch(part, branch_context) for part in parts])
    got = {item["label"]: item for item in values}
    numeric = [item for item in values if isinstance(item.get("value"), (int, float))]
    if len(numeric) < 2:
        raise runtime.Refused("could not retrieve two comparable figures for this join")
    left, right = got.get(spec.get("of")), got.get(spec.get("per"))
    if not (left and right and isinstance(left.get("value"), (int, float))
            and isinstance(right.get("value"), (int, float))):
        left, right = numeric[:2]
    operation = spec.get("compute") or "ratio"
    if operation in ("share", "ratio") and right["value"]:
        ratio = left["value"] / right["value"]
        computed = round(ratio * 100, 2) if operation == "share" else round(ratio, 4)
        outcome = {"computed": computed, "unit": "percent" if operation == "share" else "ratio",
                   "formula": f"{left['label']} / {right['label']} = {left['value']:,.0f} / {right['value']:,.0f}"}
    elif operation == "difference":
        outcome = {"computed": round(left["value"] - right["value"], 2), "unit": "difference",
                   "formula": f"{left['label']} - {right['label']} = {left['value']:,.0f} - {right['value']:,.0f}"}
    else:
        outcome = {"computed": round(sum(item["value"] for item in numeric), 2), "unit": "sum",
                   "formula": " + ".join(item["label"] for item in numeric)}
    warnings = []
    periods = {item["label"]: item.get("period") for item in numeric if item.get("period")}
    if len(set(periods.values())) > 1:
        warnings.append("the figures cover different periods (" +
                        ", ".join(f"{key}: {value}" for key, value in periods.items()) + ")")
    for item in numeric:
        if item.get("complete") is False:
            warnings.append(f"'{item['label']}' is a PARTIAL total")
        if (item.get("matched_entities") or 1) > 1:
            warnings.append(f"'{item['label']}' matched multiple separately registered entities")
    if len({item.get("source") for item in numeric}) < 2:
        warnings.append("both figures came from the same source — this is not a cross-source join")
    return {"question": question, "join": values, "compute": operation,
            "source": " + ".join(dict.fromkeys(item.get("source") or "?" for item in numeric)),
            **outcome, **({"alignment_warnings": warnings} if warnings else {})}


async def _run_generate_test_async(question, ctx, p, want=6, *, context):
    threshold, attribute = ctx.get("threshold") or {}, ctx.get("attribute") or ""
    population = ctx.get("population_type") or "organizations"
    proposed = json.loads(await llm.chat_async(
        f"Name up to {want + 4} real US {population} MOST LIKELY to satisfy: "
        f"{attribute} {threshold.get('op', '>')} {threshold.get('value')}. "
        "Use full official names. Return JSON {\"candidates\":[\"...\"]}.",
        question, context=context, json_mode=True, stage="classify"))
    candidates = [item for item in (proposed.get("candidates") or []) if isinstance(item, str)][:want + 4]
    if not candidates:
        raise runtime.Refused("could not propose candidates to test")
    import operator
    compare = {">": operator.gt, ">=": operator.ge, "<": operator.lt,
               "<=": operator.le}.get(threshold.get("op"), operator.gt)
    async def branch(candidate, branch_context):
        try:
            result = await retrieve_for(f"{attribute} for {candidate}", context=branch_context)
            value = result.get("value")
            passes = (isinstance(value, (int, float)) and threshold.get("value") is not None
                      and compare(value, float(threshold["value"])))
            return {"label": candidate, "value": value, "passes": bool(passes),
                    "source": result.get("source")}
        except (runtime.Refused, runtime.Refused) as exc:
            return {"label": candidate, "value": None, "error": str(exc)}
    tested = await _ordered(context, [
        lambda branch_context, candidate=candidate: branch(candidate, branch_context)
        for candidate in candidates])
    passing = [{"label": item["label"], "value": item["value"]}
               for item in tested if item.get("passes")][:want]
    return {"question": question, "ranking": passing, "matches": len(passing), "tested": tested,
            "threshold": f"{threshold.get('op', '>')} {threshold.get('value')}", "complete": False,
            "candidate_source": "model-proposed, then verified against the source",
            "note": "these are checked examples, not a complete population scan",
            "source": next((item.get("source") for item in tested if item.get("source")),
                           p["hit"]["title"])}


async def _materialize_async(hit, grain="county", scope="06", *, context):
    identifier = hit["identifier"]
    fm = driver.frontmatter(identifier) or {}
    operation, capability = next(((name, cap) for name, cap in
        planner.capabilities(identifier).items() if cap.get("grain") == grain), (None, None))
    if not operation:
        raise runtime.Refused(f"{hit['title']} does not serve data at {grain} grain")
    if fm.get("get") or fm.get("variable"):
        async def rows_for(variable=None):
            kwargs = {"geo": f"{grain}:*&in=state:{scope}"}
            if variable:
                kwargs["get"] = variable
            rows = await driver.accessor_async(identifier, operation, context=context, **kwargs)
            observations = []
            for row in rows[1:] if isinstance(rows, list) and len(rows) > 1 else []:
                try:
                    value = float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if value <= -100000000:
                    continue
                observations.append({"entity": store.eid("fips", row[-2] + row[-1]),
                    "entity_name": row[0], "value": value, "source": fm.get("title")})
            return observations
        observations = await rows_for()
        variable = fm.get("get", "")
        if not observations and variable.endswith("E") and not variable.endswith("PE"):
            observations = await rows_for(variable[:-1] + "PE")
        return observations, False
    result = await driver.accessor_async(identifier, operation, context=context,
        **{key: fm[key] for key in ("measureid", "get", "key") if key in fm}, n=5000)
    entity_field = capability.get("entity_field") or "locationid"
    returns, observations = capability.get("returns") or {}, []
    for row in result if isinstance(result, list) else []:
        try:
            value = float(row.get(returns.get("value") or "data_value"))
        except (TypeError, ValueError):
            continue
        if row.get(entity_field):
            observations.append({"entity": store.eid("fips", row[entity_field]),
                "entity_name": row.get(returns.get("label") or "locationname"),
                "value": value, "source": fm.get("title")})
    return observations, False


async def _run_correlate_async(question, ctx, *, context):
    spec = json.loads(await llm.chat_async(
        "Identify the TWO measures being related and population. Return JSON "
        "{\"measure_a\":\"<measure>\",\"measure_b\":\"<measure>\","
        "\"grain\":\"county|state\",\"state_fips\":\"<2-digit FIPS or empty>\"}.",
        question, context=context, json_mode=True, stage="classify"))
    measures = [item for item in (spec.get("measure_a"), spec.get("measure_b")) if item]
    found = await _ordered(context, [
        lambda branch_context, measure=measure: ard_client.search_async(
            measure, k=6, context=branch_context) for measure in measures])
    picked, seen = [], set()
    for hits in found:
        match = next((hit for hit in hits if hit["identifier"] not in seen and
                      any(cap.get("grain") == "county" for cap in
                          planner.capabilities(hit["identifier"]).values())), None)
        if match:
            picked.append(match); seen.add(match["identifier"])
    if len(picked) < 2:
        raise runtime.Refused("a correlation needs two measures available at county grain")
    scope = re.sub(r"\D", "", str(spec.get("state_fips") or "")) or "06"
    for hit in picked:
        capability = next((cap for cap in planner.capabilities(hit["identifier"]).values()
                           if cap.get("grain")), {})
        estimate = store.estimate(capability, "county", 3000)
        if (estimate.get("known") and estimate.get("blowup") and
                estimate["blowup"] > 50):
            raise runtime.Refused(
                f"materializing {hit['title']} would transfer ~{estimate['rows']:,} rows for "
                f"~3,000 counties (blowup {estimate['blowup']}x) — too expensive for one "
                "question; it should be materialized once per vintage instead")
    materialized = await _ordered(context, [
        lambda branch_context, hit=hit: _materialize_async(hit, scope=scope,
                                                            context=branch_context)
        for hit in picked])
    series, metadata = {}, []
    for hit, (observations, cached) in zip(picked, materialized):
        if not observations:
            raise runtime.Refused(f"'{hit['title']}' has no usable county-level values")
        label = hit["title"].split(" — ")[0][:40]
        series[label] = observations
        metadata.append({"measure": hit["title"], "n": len(observations), "cached": cached})
    rows, report = store.align(series)
    labels = list(series)
    if len(rows) < 3:
        raise runtime.Refused(f"only {len(rows)} units had both measures — too few to correlate")
    xs, ys = [row[labels[0]] for row in rows], [row[labels[1]] for row in rows]
    count = len(xs); mx, my = sum(xs) / count, sum(ys) / count
    sx = math.sqrt(sum((value - mx) ** 2 for value in xs))
    sy = math.sqrt(sum((value - my) ** 2 for value in ys))
    correlation = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (sx * sy) if sx and sy else 0
    return {"question": question, "correlation_r": round(correlation, 3), "n": count,
            "measures": labels, "series_meta": metadata, "join": report,
            "source": " + ".join(dict.fromkeys(hit["title"] for hit in picked)),
            "caveats": ["correlation is not causation", "this is an ecological correlation"]}


async def run(question, sites=None, assumptions=None, on_ambiguity="answer", *, context=None):
    """Complete event-loop-native engine, including every composite plan."""
    owned_clients = None
    if context is None:
        from source_clients import AsyncSourceClients
        owned_clients = await AsyncSourceClients().start()
        context = owned_clients.bind(QueryContext())
    context.usage_ledger = context.usage_ledger or llm.Ledger()
    context.discovery_ledger = context.discovery_ledger or ard_client.DiscoveryUsage()
    try:
        if on_ambiguity not in ("answer", "ask", "all"):
            on_ambiguity = "answer"
        ctx, hits = await discover_async(
            question, sites=sites, assumptions=assumptions, context=context)
        if not hits:
            raise runtime.Refused("agent finder returned no sources")
        candidates = ctx.get("entity_candidates") or []
        if (ctx.get("entity_status") or "").lower() == "ambiguous" and len(candidates) > 1:
            return _entity_clarification(
                question, ctx, candidates, context.usage_ledger, context.discovery_ledger)
        shape = ctx.get("shape") if ctx.get("shape") in planner.SHAPES else "point"
        if len(ctx.get("interpretations") or []) < 2:
            linked = await _link_entity_async(ctx, context=context)
            linked_candidates = [entity for entity in linked if entity]
            if len(linked_candidates) > 1:
                return _entity_clarification(
                    question, ctx, linked_candidates, context.usage_ledger, context.discovery_ledger)
        intent = QueryIntent.from_context(question, ctx, sites)

        if len(ctx.get("interpretations") or []) >= 2 and shape in ("point", "status", "entity-list"):
            data = await _run_ambiguous_async(question, ctx, context=context)
            clarification = _clarification(ctx.get("attribute") or "the requested measure",
                                           ctx.get("entity") or "", data.get("interpretations") or [])
            if clarification:
                trace = [attempt for option in data.get("interpretations") or []
                         for attempt in option.get("attempts") or []]
                return await _ambiguity_result(
                    question, ctx, hits, intent, clarification, on_ambiguity,
                    context.usage_ledger, context.discovery_ledger, trace, context=context)
            hit = hits[0]
            evidence, attempts = await _admit_async(intent, hit, data, context=context)
            answer, renderer = await _present_async(question, evidence, context=context)
            return {"question": question, "answer": answer, "shape": shape,
                    "usage": context.usage_ledger.snapshot(),
                    "discovery_usage": context.discovery_ledger.snapshot(),
                    "intent": intent.to_dict(), "attempts": [item.to_dict() for item in attempts],
                    "evidence": evidence.to_dict(), "answer_renderer": renderer,
                    "plan": f"ambiguous measure → {len(data['interpretations'])} interpretations answered separately",
                    "source": {"identifier": hit["identifier"], "title": hit["title"],
                               "publisher": hit.get("publisher")},
                    "candidates": [{"identifier": item.get("identifier"), "title": item["title"],
                                    "score": item["score"], "publisher": item.get("publisher")}
                                   for item in hits], "data": data}

        plan = planner.plan(shape, hits, ctx.get("quantifier") or "exhaustive")
        await _asay(context, "plan_chosen", shape=shape, verdict=plan["verdict"],
                    why=plan.get("why", ""), summary=planner.describe(shape, plan))
        grant_hit = next((hit for hit in ([plan["hit"]] if plan.get("hit") else []) + hits[:2]
                          if (driver.frontmatter(hit["identifier"]) or {}).get("irsgrants")), None)
        state = None
        if grant_hit:
            import grants as grants_module
            data = await _run_grants_async(question, ctx, context=context)
            grant_leaf = {"forward": "grants-made", "reverse": "grants-received",
                          "ranking": "top-grantmakers", "biggest_recipients": "biggest-recipients",
                          "geo": "geographic", "overview": "grant-overview",
                          "shared": "shared-grantees", "theme": "grants-by-cause"}
            identifier = ("sources/irs-grants/" +
                          grant_leaf.get(_grant_direction(question, ctx, grants_module), "grants-made") + ".md")
            hit = next((item for item in hits if item["identifier"] == identifier), None)
            if not hit:
                fm = driver.frontmatter(identifier) or {}
                hit = {"identifier": identifier, "title": fm.get("title", grant_hit["title"]),
                       "publisher": grant_hit.get("publisher")}
        elif plan["verdict"] == "infeasible":
            need = ("a source that can see a whole population"
                    if shape in ("ranking", "aggregate", "filtered-subset")
                    else "a capability none of the matching sources declare")
            raise runtime.Refused(f"this is a '{shape}' question, which needs {need}; {plan['why']}.")
        elif plan["verdict"] == "compose:materialize-and-correlate":
            data = await _run_correlate_async(question, ctx, context=context); hit = plan["hit"]
        elif plan["verdict"] == "compose:derive":
            data = await _run_derive_async(question, ctx, context=context); hit = plan["hit"]
        elif plan["verdict"] == "compose:generate-and-test":
            data = await _run_generate_test_async(question, ctx, plan, context=context); hit = plan["hit"]
        elif plan["verdict"].startswith("compose:fan-out"):
            data = await _run_fanout_async(question, ctx, shape, context=context); hit = plan["hit"]
        elif (plan["verdict"].startswith("compose:scan-and") or
              shape in ("ranking", "aggregate", "filtered-subset")):
            hit = plan["hit"]
            if (driver.frontmatter(hit["identifier"]) or {}).get("bq"):
                data = await _run_bq_async(question, ctx, plan, context=context)
            else:
                data = await _run_ranking_async(question, ctx, plan, context=context)
        else:
            ctx, hits, hit, _tried, data, state = await _search_async(
                question, ctx=ctx, hits=hits, context=context)

        resolution = data.pop("_ambiguity", None) if isinstance(data, dict) else None
        if resolution:
            clarification = _clarification(resolution.get("attribute") or intent.measure,
                                           intent.entity or "", resolution.get("options") or [])
            if clarification and on_ambiguity in ("ask", "all"):
                ordered_hits = [hit] + [item for item in hits
                                        if item.get("identifier") != hit.get("identifier")]
                trace = [item.to_dict() for item in ((state or {}).get("_attempts") or [])]
                return await _ambiguity_result(
                    question, ctx, ordered_hits, intent, clarification, on_ambiguity,
                    context.usage_ledger, context.discovery_ledger, trace, context=context)
            if clarification:
                data["ambiguity"] = {"attribute": clarification.attribute,
                                     "reason": resolution.get("reason"),
                                     "options": clarification.to_dict()["options"]}
        hit = _cite_concept_actually_used(hit, data)
        if state and state.get("_evidence"):
            evidence, attempts = state["_evidence"], state.get("_attempts") or []
            evidence.identifier = hit["identifier"]
            evidence.provenance["source_document"] = hit["identifier"]
        else:
            evidence, attempts = await _admit_async(intent, hit, data, context=context)
        answer, renderer = await _present_async(question, evidence, context=context)
        return {"question": question, "answer": answer,
                "usage": context.usage_ledger.snapshot(),
                "discovery_usage": context.discovery_ledger.snapshot(), "shape": shape,
                "intent": intent.to_dict(), "attempts": [attempt.to_dict() for attempt in attempts],
                "evidence": evidence.to_dict(), "answer_renderer": renderer,
                "plan": planner.describe(shape, plan),
                "source": {"identifier": hit["identifier"], "title": hit["title"],
                           "publisher": hit.get("publisher")},
                "candidates": [{"identifier": candidate.get("identifier"),
                                "title": candidate["title"], "score": candidate["score"],
                                "publisher": candidate.get("publisher")} for candidate in hits],
                "data": data}
    finally:
        if owned_clients is not None:
            await owned_clients.close()






def _leaf_for_concept(concept):
    """The SEC leaf that pins a given us-gaap concept, keyed off the built index metadata."""
    return driver._concept_meta(concept)


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








PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Neural KG</title>
<style>
 body{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#1a1a1a}
 h1{font-size:1.5rem;margin-bottom:.1em} .byline{color:#666;font-size:.95rem;margin:0 0 .6em;letter-spacing:.02em} .sub{color:#666;margin-top:0}
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
<h1>Neural KG</h1>
<p class="byline"><a href="https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf" style="color:inherit;text-decoration:underline;text-decoration-color:#bbb;text-underline-offset:3px">OKF</a> + <a href="https://agenticresourcediscovery.org/" style="color:inherit;text-decoration:underline;text-decoration-color:#bbb;text-underline-offset:3px">ARD</a></p>
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
          +esc(d.intent.period||'latest')+'</p>';
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
    .replace("<title>Neural KG</title>", "<title>Data for Nonprofits — a TechSoup view</title>")
    .replace('<h1>Neural KG</h1>',
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
    if k == "entity_detected":
        mention, canonical = ev.get("entity") or "", ev.get("canonical") or ""
        status, kind = ev.get("status") or "none", ev.get("type") or "none"
        if not mention and not canonical:
            return "🏷️ Entity detection: no named entity"
        label = f"“{mention}”"
        if canonical and canonical.casefold() != mention.casefold():
            label += f" → {canonical}"
        suffix = f" · {kind}" + (f" · {status}" if status not in ("", "resolved") else "")
        return "🏷️ Entity detection: " + label + suffix
    if k == "property_identified":
        properties = ([ev.get("attribute")] if ev.get("attribute") else
                      list(ev.get("interpretations") or []))
        label = " / ".join(str(item) for item in properties[:3] if item) or "not identified"
        return (f"🔎 Property identification: {label} · {ev.get('period') or 'latest'}"
                f" · {ev.get('shape') or 'point'}")
    if k == "plan":
        sources = list(ev.get("sources") or [])
        listed = ", ".join(sources[:4]) + (", …" if len(sources) > 4 else "")
        family_label = "source family" if len(sources) == 1 else "source families"
        return (f"🧭 Initial plan: {ev.get('shape') or 'point'} · {ev.get('period') or 'latest'}"
                f" · search {len(sources)} {family_label}" + (f" ({listed})" if listed else ""))
    if k == "candidates":
        items = list(ev.get("items") or [])
        summaries = []
        for item in items[:3]:
            score = item.get("score")
            score_text = f"{score:g}" if isinstance(score, (int, float)) else str(score or "?")
            summaries.append(f"{item.get('title') or 'untitled'} ({score_text}, "
                             f"{item.get('publisher') or 'unknown'})")
        count = ev.get("count") if ev.get("count") is not None else len(items)
        table_label = "candidate table" if count == 1 else "candidate tables"
        return (f"📚 ARD summary: {count} {table_label}" +
                (" · top: " + "; ".join(summaries) if summaries else ""))
    if k == "plan_chosen":
        return "🧭 Execution plan: " + (ev.get("summary") or ev.get("verdict") or "")
    if k == "entity_mapping":
        phase, name = ev.get("phase"), ev.get("canonical") or ev.get("mention") or "the entity"
        if phase == "searching":
            return f"🔗 Entity identifier mapping: searching crosswalk records for “{name}”…"
        if phase == "candidates":
            count = ev.get("count") or 0
            return (f"🔗 Entity identifier mapping: {count} crosswalk candidate"
                    f"{'s' if count != 1 else ''} found; checking against the full question…")
        if phase == "mapped":
            identity = ev.get("label") or name
            if ev.get("qid"):
                identity += f" · Wikidata {ev['qid']}"
            return "🔗 Entity identifier mapping: crosswalk match · " + identity
        if phase == "ambiguous":
            return (f"🔗 Entity identifier mapping: {ev.get('count') or 0} plausible records remain"
                    "; clarification is needed")
        if phase == "skipped":
            return "⏭️ Entity identifier mapping: skipped · " + (ev.get("reason") or "not needed")
        if phase == "not_found":
            return ("⚠️ Entity identifier mapping: no crosswalk found for “" + name +
                    "”; source-specific identifiers will be tried")
    if k == "resolve":
        return f"🧩 resolved “{ev.get('mention','')}” → {ev.get('label','')}"
    return ""




HOW_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How Neural KG works</title>
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
    question = " ".join(argv) or "How much did Apple spend on R&D in 2023?"
    res = asyncio.run(run(question))
    print(json.dumps(res, indent=2))
    _print_cost_report(res.get("usage") or {}, res.get("discovery_usage") or {})


if __name__ == "__main__":
    main(sys.argv[1:])
