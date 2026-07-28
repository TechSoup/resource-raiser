#!/usr/bin/env python3
"""Generate the test-query corpus.

Queries are built from the REAL leaf inventory (actual 990 fields, CDC measures, Census
variables, SEC concepts) crossed with real entities, so a failure means the engine is
wrong rather than the question being about data we never had. Each case declares:

    shape   the query shape the classifier should pick
    expect  "answer" -> a grounded answer | "refuse" -> refused before fetching |
            "either" -> acceptable either way (coverage/data-dependent)
    dirs    (point/entity-list only) the source dir the answer SHOULD route to — used to
            assert POINT-LOOKUP COVERAGE of every database, and checkable at run time.

Two goals this file is organized around:
  1. POINT lookups exercise EVERY source (see COVERAGE report printed on generate).
  2. A broad NON-POINT set: comparison, ranking, filtered-subset, ratio, correlation,
     timeseries, aggregate, entity-list, topical, and ambiguous-measure fan-out.

    python3 tests/gen_queries.py
"""
import os, glob, json, random
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queries.json")
random.seed(11)

# --- real entities ---------------------------------------------------------------
NONPROFITS = ["the American Red Cross", "Feeding America", "the Sierra Club", "the Wikimedia Foundation",
              "the Nature Conservancy", "Habitat for Humanity", "the ACLU Foundation", "St. Jude",
              "Doctors Without Borders", "the Salvation Army", "Goodwill Industries", "the YMCA",
              "the American Cancer Society", "the Humane Society", "United Way"]
UNIVERSITIES = ["Stanford", "MIT", "Harvard University", "Johns Hopkins University", "Caltech",
                "the University of Michigan", "Duke University", "Yale University",
                "the University of Washington", "Columbia University"]
COMPANIES = ["Apple", "Microsoft", "Tesla", "NVIDIA", "Amazon", "Alphabet", "Intel", "Ford",
             "Walmart", "Netflix", "Coca-Cola", "Boeing", "Pfizer", "Starbucks", "Disney",
             "JPMorgan Chase", "ExxonMobil", "Costco", "AMD", "General Motors"]
PLACES = ["Chicago", "Los Angeles", "Detroit", "Miami", "Houston", "Seattle", "Boston",
          "Phoenix", "Denver", "Atlanta", "Cleveland", "Portland", "Nashville", "Baltimore",
          "San Antonio", "Milwaukee"]
STATES = ["California", "New York", "Texas", "Florida", "Illinois", "Ohio", "Georgia",
          "Pennsylvania", "Arizona", "Michigan"]


# --- phrasing variety: rotate templates so no two queries read the same way -------
def phr(templates, **kw):
    return random.choice(templates).format(**kw)


MEASURE_PHR = [                                     # {e} entity, {m} measure
    "What was {e}'s {m}?", "What is {e}'s {m}?", "How much {m} did {e} report?",
    "{e}'s {m}", "Report {e}'s {m}.", "Tell me {e}'s latest {m}.",
    "What did {e} report for {m}?", "{e} {m}, most recent year", "Give me {e}'s {m}.",
    "Look up the {m} for {e}.",
]
PLACE_PHR = [                                       # {m} measure, {p} place
    "{m} in {p}", "What is the {m} in {p}?", "What's the {m} for {p}?",
    "How high is the {m} in {p}?", "Tell me {p}'s {m}.", "{p} {m}",
    "Look up the {m} in {p}.", "What's the {m} like in {p}?",
]


def leaf_titles(src, n, strip=" — "):
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "sources", src, "*.md"))):
        if os.path.basename(p) == "_access.md":
            continue
        try:
            fm = yaml.safe_load(open(p, encoding="utf-8").read().split("---")[1]) or {}
        except Exception:
            continue
        t = (fm.get("title") or "").split(strip)[0].strip()
        if t and len(t) < 60:
            out.append(t)
    random.shuffle(out)
    return out[:n]


CASES = []


def add(q, shape, expect="answer", why="", dirs=None):
    CASES.append({"q": q, "shape": shape, "expect": expect,
                  **({"why": why} if why else {}), **({"dirs": dirs} if dirs else {})})


# =================================================================================
# POINT LOOKUPS — one section per source, so every database is covered. Each is
# tagged with dirs=[source] for the coverage assertion below.
# =================================================================================

# --- sec-edgar: curated headline concepts (not random leaves — most aren't reported) x companies.
# Each company gets a DIFFERENT measure and a rotated phrasing — no two read the same.
SEC_MEASURES = ["total revenue", "net income", "total assets", "total liabilities",
                "research and development expense", "operating income", "gross profit",
                "cash and cash equivalents", "stockholders equity", "diluted earnings per share",
                "cost of revenue", "operating cash flow", "long-term debt", "inventory",
                "selling, general and administrative expense", "capital expenditures"]
for i, c in enumerate(COMPANIES):
    add(phr(MEASURE_PHR, e=c, m=SEC_MEASURES[i % len(SEC_MEASURES)]), "point", dirs=["sec-edgar"])
for i, m in enumerate(SEC_MEASURES):                # second pass: measures crossed with OTHER companies
    add(phr(MEASURE_PHR, e=COMPANIES[(i + 7) % len(COMPANIES)], m=m), "point", dirs=["sec-edgar"])
for c in random.sample(COMPANIES, 6):               # a few year-scoped
    add(f"What was {c}'s {random.choice(['net income', 'total revenue', 'operating income'])} in 2023?",
        "point", dirs=["sec-edgar"])

# --- treasury: national fiscal figures (no entity) + real leaf series
add("What is the US national debt?", "point", dirs=["treasury"])
add("What is the current US national debt?", "point", dirs=["treasury"])
for cur in ["Euro", "Japanese yen", "British pound", "Canadian dollar", "Mexican peso"]:
    add(f"{cur} to dollar exchange rate", "point", dirs=["treasury"])
for m in leaf_titles("treasury", 6):
    add(f"What is the {m}?", "point", dirs=["treasury"])

# --- census: curated ACS measures (map to DP variables) x places
CENSUS_MEASURES = ["median household income", "poverty rate", "unemployment rate", "median age",
                   "median home value", "median gross rent", "percentage with a bachelor's degree",
                   "percentage without health insurance", "per capita income", "median household size",
                   "homeownership rate", "percentage of households with broadband",
                   "percentage of people who walk to work", "percentage foreign-born"]
for m in CENSUS_MEASURES:                           # each measure at a different place, rotated phrasing
    add(phr(PLACE_PHR, m=m, p=random.choice(PLACES)), "point", dirs=["census"])
for m in random.sample(CENSUS_MEASURES, 6):         # a second, differently-placed pass
    add(phr(PLACE_PHR, m=m, p=random.choice(PLACES)), "point", dirs=["census"])
for st in random.sample(STATES, 3):
    add(phr(PLACE_PHR, m=random.choice(["median household income", "poverty rate", "median home value"]),
            p=st), "point", dirs=["census"])

# --- cdc-places: real health-measure leaves x places, rotated phrasing
for m in leaf_titles("cdc-places", 16):
    add(phr(PLACE_PHR, m=m, p=random.choice(PLACES)), "point", dirs=["cdc-places"])

# --- nonprofit-990: real 990 financial fields x orgs
NP_FIELDS = ["total revenue", "total expenses", "total assets", "net assets",
             "total contributions and grants", "program service revenue", "investment income",
             "officer compensation", "salaries and wages", "payroll taxes", "net fundraising income",
             "professional fundraising fees"]
for org, f in zip(NONPROFITS, NP_FIELDS):
    add(phr(MEASURE_PHR, e=org, m=f), "point", dirs=["nonprofit-990"])
for org in NONPROFITS[:6]:
    add(phr(MEASURE_PHR, e=org, m=random.choice(["total revenue", "total expenses", "net assets"])),
        "point", dirs=["nonprofit-990"])

# --- nonprofit-bmf: registration facts (each of the 5 leaves) x orgs
for org in NONPROFITS[:5]:
    add(f"Where is {org} headquartered?", "point", dirs=["nonprofit-bmf", "nonprofit-profile"])
    add(f"What sector does {org} work in?", "point", dirs=["nonprofit-bmf"])
add("When did the Sierra Club become tax-exempt?", "point", dirs=["nonprofit-bmf"])
add("What is the Nature Conservancy's IRS ruling date?", "point", dirs=["nonprofit-bmf"])

# --- nonprofit-profile: each of the 7 Wikidata/Wikipedia leaves x orgs
for org in NONPROFITS[:5]:
    add(f"When was {org} founded?", "point", dirs=["nonprofit-profile"])
add("Who is the CEO of the Wikimedia Foundation?", "point", dirs=["nonprofit-profile"])
add("Who founded the Sierra Club?", "point", dirs=["nonprofit-profile"])
add("How many employees does the Wikimedia Foundation have?", "point", dirs=["nonprofit-profile"])
add("What is the ACLU's website?", "point", dirs=["nonprofit-profile"])
add("What does Feeding America do?", "point", dirs=["nonprofit-profile"])
add("Give me an overview of the Nature Conservancy", "point", dirs=["nonprofit-profile"])

# --- usaspending: federal funding received x orgs (harness reports a single total)
for org in NONPROFITS[:6]:
    add(f"How much federal funding has {org} received?", "point", dirs=["usaspending"])

# =================================================================================
# STATUS (boolean / category) — nonprofit-990 + nonprofit-bmf
# =================================================================================
for org in NONPROFITS[:8]:
    add(f"Is {org} a 501(c)(3)?", "status", dirs=["nonprofit-990"])
for org in NONPROFITS[:5]:
    add(f"Are donations to {org} tax-deductible?", "status", dirs=["nonprofit-bmf"])
for org in NONPROFITS[:4]:
    add(f"Is {org} a private foundation or a public charity?", "status", dirs=["nonprofit-bmf"])

# =================================================================================
# ENTITY-LIST — nih-reporter + nsf-awards (their natural shape; covers those DBs)
# =================================================================================
for u in UNIVERSITIES:
    add(f"NSF research awards for {u}", "entity-list", dirs=["nsf-awards"])
for u in UNIVERSITIES:
    add(f"How much NIH research funding does {u} receive?", "entity-list", dirs=["nih-reporter"])

# =================================================================================
# TOPICAL — grants-gov (covers that DB)
# =================================================================================
for topic in ["education", "medical research", "housing", "the arts", "climate", "food security",
              "youth programs", "disaster relief", "mental health", "rural development"]:
    add(f"What grants can a nonprofit apply for in {topic}?", "topical", dirs=["grants-gov"])

# =================================================================================
# COMPARISON (K named entities) — across companies, universities, nonprofits, places
# =================================================================================
for a, b in [(UNIVERSITIES[i], UNIVERSITIES[i + 1]) for i in range(0, 8, 2)]:
    add(f"Does {a} or {b} get more NIH funding?", "comparison")
for a, b in [(UNIVERSITIES[i], UNIVERSITIES[j]) for i, j in [(0, 4), (1, 3), (5, 7), (6, 9)]]:
    add(f"Compare NSF research awards for {a} and {b}", "comparison")
for a, b in [(NONPROFITS[i], NONPROFITS[i + 1]) for i in range(0, 10, 2)]:
    add(f"Compare the total revenue of {a} and {b}", "comparison")
for a, b in [(NONPROFITS[i], NONPROFITS[j]) for i, j in [(0, 4), (2, 7), (6, 9)]]:
    add(f"Which has more total assets, {a} or {b}?", "comparison")
for a, b in [("Apple", "Microsoft"), ("Tesla", "Ford"), ("NVIDIA", "Intel"), ("Amazon", "Walmart")]:
    add(f"Which had higher total revenue, {a} or {b}?", "comparison")
for a, b in [(PLACES[i], PLACES[i + 1]) for i in range(0, 8, 2)]:
    add(f"Compare the poverty rate in {a} and {b}", "comparison")
for a, b in [("Chicago", "Houston"), ("Miami", "Seattle")]:
    add(f"Which has a higher diabetes rate, {a} or {b}?", "comparison")

# =================================================================================
# RANKING (open population)
# =================================================================================
for m in leaf_titles("cdc-places", 12):
    add(f"Which county has the highest rate of {m.lower()}?", "ranking",
        why="CDC PLACES declares server-side ordering")
add("Which city has the highest diabetes rate?", "ranking")
add("Which city has the lowest obesity rate?", "ranking")
add("Which organization receives the most federal funding?", "ranking")
# refusals: an entity-list source cannot see its population, and some measures have no source at all
for pop, src in [("university", "NIH"), ("nonprofit", "IRS 990"), ("state", "NIH")]:
    add(f"Which {pop} gets the most funding from {src}?", "ranking", "refuse",
        why=f"{src} is entity-list only; it cannot see its population")
for q in ["Which company has the most employees?", "Which university has the largest endowment?"]:
    add(q, "ranking", "refuse", why="no source can see this population/measure")
# SERVED by the credential-gated BigQuery population sources (irs-990-bq / sec-bq): answered when
# GOOGLE_CLOUD_PROJECT is set, refused when it isn't -> 'either'.
for q in ["What is the largest nonprofit in the US?", "Which nonprofit has the highest revenue?",
          "Rank US companies by revenue", "Which nonprofit CEO is paid the most?",
          "Which company gets the most funding from SEC?"]:
    add(q, "ranking", "either", why="answerable via the BigQuery population sources when enabled")

# =================================================================================
# FILTERED-SUBSET (numeric threshold)
# =================================================================================
for m, t in [("diabetes", 20), ("obesity", 40), ("binge drinking", 25), ("high blood pressure", 35),
             ("current asthma", 12)]:
    add(f"Which cities have a {m} rate above {t}%?", "filtered-subset")
add("Which cities have a diabetes rate below 8%?", "filtered-subset")
add("Which counties have an obesity rate over 45%?", "filtered-subset")
# refusals: exhaustive membership over a population the source cannot enumerate
add("Which universities get more than a billion dollars a year from NIH?", "filtered-subset", "refuse")
add("Which nonprofits have revenue over $1 billion?", "filtered-subset", "either",
    why="served by irs-990-bq when GOOGLE_CLOUD_PROJECT is set")
add("List all nonprofits in Chicago", "filtered-subset", "refuse",
    why="ProPublica declares population.enumerable: false")
# existential: propose-and-verify allowed where per-entity values are complete
add("Give me some universities that get more than a billion dollars from NIH", "filtered-subset")
add("Name a few universities receiving over $500 million from NIH", "filtered-subset")
add("Give me some companies with over $200 billion in revenue", "filtered-subset", "either")

# =================================================================================
# RATIO / CROSS-SOURCE JOIN
# =================================================================================
for org in NONPROFITS[:8]:
    add(f"What share of {org}'s revenue comes from federal funding?", "ratio")
for org in NONPROFITS[:4]:
    add(f"How does {org}'s revenue compare to the federal funding it receives?", "ratio")
for u in UNIVERSITIES[:4]:
    add(f"How does {u}'s NIH funding compare to its NSF funding?", "ratio")
add("What fraction of the Red Cross's revenue is program service revenue?", "ratio")

# =================================================================================
# CORRELATION (materialized)
# =================================================================================
CORR = [("median household income", "diabetes"), ("poverty rate", "obesity"),
        ("median household income", "obesity"), ("poverty rate", "diabetes"),
        ("unemployment rate", "diabetes"), ("poverty rate", "current lack of health insurance"),
        ("median household income", "binge drinking")]
for a, b in CORR:
    add(f"Across California counties, is {a} correlated with {b} rates?", "correlation")
for st in STATES[:4]:
    add(f"Across {st} counties, is median household income correlated with obesity rates?", "correlation")
add("Do richer counties in California have lower diabetes rates?", "correlation")

# =================================================================================
# TIMESERIES (fan-out over periods)
# =================================================================================
for c in COMPANIES[:5]:
    add(f"{c} total revenue from 2019 to 2023", "timeseries", "either",
        why="fan-out over periods; correctness matters more than latency")
add("How has the American Red Cross's revenue changed since 2019?", "timeseries", "either")

# =================================================================================
# AGGREGATE (mostly unserved -> refuse)
# =================================================================================
# irs-990-bq can count/sum the nonprofit filer population when enabled -> 'either'
add("How many 501(c)(3) organizations are there in the US?", "aggregate", "either",
    why="irs-990-bq counts the filer population when GOOGLE_CLOUD_PROJECT is set")
add("What is the total revenue of all US nonprofits?", "aggregate", "either")
add("How many nonprofits are headquartered in California?", "aggregate", "either")
# still unserved: NIH/USAspending cannot aggregate their whole population
add("How much does NIH award in total each year?", "aggregate", "refuse")
add("What is the total federal funding to all nonprofits?", "aggregate", "refuse")
add("What is the average diabetes rate across US counties?", "aggregate", "either",
    why="CDC can enumerate, so an average is computable if the aggregate plan is wired")

# =================================================================================
# AMBIGUOUS MEASURE (separate answer per interpretation)
# =================================================================================
for c in COMPANIES[:5]:
    add(f"What were {c}'s earnings?", "point", "either",
        why="'earnings' -> net income / operating income / EBITDA / gross profit")
for c in COMPANIES[:4]:
    add(f"How big is {c}?", "point", "either", why="'how big' -> revenue / assets / employees / net income")
add("What was Apple's profit?", "point", "either", why="'profit' is ambiguous")
add("Tell me about Microsoft's performance", "point", "either")

# =================================================================================
# EDGE CASES & KNOWN TRAPS (named regressions from prior bugs)
# =================================================================================
add("NSF research awards for MIT", "entity-list", dirs=["nsf-awards"],
    why="must resolve to Massachusetts Institute of Technology, NOT 'MIT Development Foundation Inc'")
add("NSF research awards for Caltech", "entity-list", dirs=["nsf-awards"],
    why="abbreviation must resolve to the canonical name or NSF returns 0")
add("How much federal funding has the American Red Cross received?", "point", dirs=["usaspending"],
    why="name-matched across ~7 chapters; the answer must disclose that scope")
add("How much NIH research funding does Johns Hopkins receive?", "entity-list", dirs=["nih-reporter"],
    why="must page every FY project (~$969M), not just the top 10 (~$208M)")
add("Is the Red Cross a 501(c)(3)?", "status", dirs=["nonprofit-990"], why="informal name must resolve")
add("What is the poverty rate in Chicago?", "point", dirs=["census"],
    why="ACS jam values must be filtered, not reported")
add("What was Apple's diluted earnings per share?", "point", dirs=["sec-edgar"],
    why="per-share concepts live in units.USD/shares; must not pick basic")
add("How much did Apple spend on R&D in 2023?", "point", dirs=["sec-edgar"])


def _report(uniq):
    from collections import Counter
    cov = Counter()
    for c in uniq:
        for d in c.get("dirs", []):
            cov[d] += 1
    all_srcs = sorted(os.path.basename(os.path.dirname(p))
                      for p in glob.glob(os.path.join(ROOT, "sources", "*", "_access.md")))
    print("\nPOINT/DIRECT-LOOKUP COVERAGE (queries tagged per source):")
    missing = []
    for s in all_srcs:
        n = cov.get(s, 0)
        print(f"  {s:18} {n:>4}" + ("   <-- NOT COVERED" if n == 0 else ""))
        if n == 0:
            missing.append(s)
    print("  " + ("ALL SOURCES COVERED" if not missing else f"MISSING: {missing}"))


if __name__ == "__main__":
    seen, uniq = set(), []
    for c in CASES:
        if c["q"].lower() not in seen:
            seen.add(c["q"].lower())
            uniq.append(c)
    random.shuffle(uniq)                            # interleave shapes/sources so no long run of look-alikes
    by_shape, by_expect = {}, {}
    for c in uniq:
        by_shape[c["shape"]] = by_shape.get(c["shape"], 0) + 1
        by_expect[c["expect"]] = by_expect.get(c["expect"], 0) + 1
    json.dump({"n": len(uniq), "by_shape": by_shape, "by_expect": by_expect, "cases": uniq},
              open(OUT, "w"), indent=1)
    print(f"wrote {len(uniq)} test queries -> {OUT}\n")
    print("by shape:")
    for s, n in sorted(by_shape.items(), key=lambda x: -x[1]):
        print(f"  {s:18} {n:>4}")
    print("\nby expectation:")
    for e, n in sorted(by_expect.items(), key=lambda x: -x[1]):
        print(f"  {e:10} {n:>4}")
    _report(uniq)
