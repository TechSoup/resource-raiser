#!/usr/bin/env python3
"""Deterministic, LLM-free toolbelt for the ARD data-query skill.

The HOST chatbot does all the reasoning (extract entity/attribute/period, disambiguate, decide whether
a result answers the question, backtrack, synthesize). These subcommands do only the mechanical work,
so NO LLM tokens are spent here — the one exception is `find`, which calls the Agent Finder service
(that service runs its own embedding + rerank models; that is the intended, isolated token cost).

  ard.py sources                               # the data sources + what each covers
  ard.py find "<attribute text>" [dir,dir]     # rank OKF leaves (optionally scoped to source dirs)
  ard.py resolve "<name>"                       # canonical ids/keys (CIK/EIN/FIPS) + place granularity
  ard.py fetch <leaf-identifier> [k=v ...]      # pull the datum from one leaf (host supplies the keys)

Every subcommand prints JSON on stdout; on failure it prints {"error": "..."} and exits non-zero,
so the host can read the reason and backtrack.
"""
import sys, os, re, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import driver, ard_client, resolver
import harness                                                   # Azure-free at import (lazy chat client)


class _Done(Exception):
    def __init__(self, obj, code):
        self.obj, self.code = obj, code


def out(obj, code=0):
    """Finish a subcommand. Raises a dedicated signal (NOT SystemExit) so it isn't swallowed by the
    `except SystemExit` that catches accessor/network failures inside cmd_fetch."""
    raise _Done(obj, code)


def cmd_sources():
    out({"sources": harness._sources_catalog()})


def cmd_examples():
    cat = harness._sources_catalog()
    out({"examples": [{"source": s["name"], "covers": s["covers"], "queries": s["examples"]}
                      for s in cat if s["examples"]]})


def cmd_find(argv):
    if not argv:
        out({"error": "usage: find \"<text>\" [dir,dir]"}, 2)
    text = argv[0]
    sources = [s for s in argv[1].split(",") if s] if len(argv) > 1 else None
    hits = ard_client.search(text, k=8, sources=sources)
    out({"query": text, "sources": sources, "hits": hits})


def cmd_resolve(argv):
    if not argv:
        out({"error": "usage: resolve \"<name>\""}, 2)
    mention = argv[0]
    try:
        cands = resolver._search(mention)
    except Exception as e:
        out({"error": f"wikidata search failed: {e}"}, 1)
    results = []
    for c in cands[:6]:
        try:
            label, keys = resolver._claims(c["id"])
        except Exception:
            label, keys = c.get("label"), {}
        entry = {"qid": c["id"], "label": label, "description": c.get("description", ""), "keys": keys}
        # for a place, include the containment chain (place -> county -> state) as granularity options,
        # each with the census geography clause the host can hand straight to `fetch`
        if any(k.startswith("fips") for k in keys):
            entry["place_levels"] = [{"label": lv.get("label"), "geo": harness._geo_from_fips(lv["keys"])}
                                     for lv in resolver.hierarchy(c["id"]) if harness._geo_from_fips(lv["keys"])]
        results.append(entry)
    out({"mention": mention, "candidates": results})


def _kv(argv):
    return dict(a.split("=", 1) for a in argv if "=" in a)


def cmd_fetch(argv):
    if not argv:
        out({"error": "usage: fetch <leaf-identifier> [k=v ...]"}, 2)
    identifier = argv[0]
    p = _kv(argv[1:])
    period = p.get("period", "latest")
    try:
        fm = driver.frontmatter(identifier)
    except Exception as e:
        out({"error": f"no such leaf {identifier}: {e}"}, 1)
    try:
        if fm.get("concept"):                                    # SEC EDGAR — one XBRL concept for a filer
            cik = p.get("cik") or (driver.ticker_to_cik(p["ticker"])[0] if p.get("ticker") else None)
            if not cik:
                out({"error": "need cik= (from resolve) or ticker="}, 1)
            data = driver.accessor(identifier, "company_concept", cik=str(int(cik)))
            unit, rows = driver._select_unit(data.get("units", {}), fm.get("unit", "currency"))
            if not rows:
                out({"error": "company does not report this concept in its expected unit"}, 1)
            row = driver.pick_value(rows, period, fm.get("periodType", "duration"),
                                    strict=(len(re.sub(r"\D", "", period)) == 4))
            if not row:
                out({"error": f"reported, but not for period {period}"}, 1)
            out({"company": data.get("entityName"), "metric": fm["title"].split(" — ")[0],
                 "concept": "us-gaap:" + fm["concept"], "period": "FY" + row["end"][:4],
                 "period_end": row["end"], "value": row["val"], "unit": unit, "source": "SEC EDGAR"})

        if fm.get("classification"):                             # nonprofit 501(c) status
            import nonprofit
            ein = re.sub(r"\D", "", p.get("ein", ""))            # ProPublica keys on the dashless EIN
            if not ein:
                out({"error": "need ein="}, 1)
            out(nonprofit.classify(ein))

        if fm.get("field"):                                      # nonprofit 990 numeric field
            import nonprofit
            org = re.sub(r"\D", "", p["ein"]) if p.get("ein") else (p.get("org") or p.get("name"))
            if not org:
                out({"error": "need ein= (from resolve) or org="}, 1)
            out(nonprofit.fetch_np(fm["field"], org, period))

        if fm.get("variable"):                                   # US Census ACS variable for a geography
            geo = p.get("geo") or harness._geo_from_fips(p)      # host passes geo clause or fips_* keys
            if not geo:
                out({"error": "need geo= clause or fips_place=/fips_county=/fips_state="}, 1)
            arr = driver.accessor(identifier, "acs", geo=geo)
            if not isinstance(arr, list) or len(arr) < 2:
                out({"error": "no census row for that geography"}, 1)
            val = arr[1][1]
            try:
                if float(val) <= -100000000:
                    out({"error": "census suppressed/unavailable value for that geography"}, 1)
            except (TypeError, ValueError):
                pass
            out({"place": arr[1][0], "metric": fm["title"].split(" — US Census")[0],
                 "variable": fm["variable"], "value": val, "source": "US Census ACS"})

        if fm.get("measureid"):                                  # CDC PLACES health measure for a place
            place = p.get("place")
            if not place:
                out({"error": "need place= (a county/city name)"}, 1)
            arr = driver.accessor(identifier, "by_measure", measureid=fm["measureid"], place=place)
            row = next((r for r in arr if r.get("data_value")), None) if isinstance(arr, list) else None
            if not row:
                out({"error": f"no CDC PLACES row for place {place!r}"}, 1)
            out({"place": row.get("locationname"), "measure": fm["title"].split(" — CDC")[0],
                 "value": row.get("data_value"), "unit": row.get("data_value_unit"), "source": "CDC PLACES"})

        if fm.get("tfield"):                                     # US Treasury FiscalData field/series
            q = f"fields={fm['tfield']},record_date&sort=-record_date&page[size]=1"
            if fm.get("filter"):
                q += f"&filter={fm['filter']}"
            rows = (driver.accessor(identifier, "get", query=q) or {}).get("data", [])
            if not rows:
                out({"error": "no treasury data"}, 1)
            rec = {"metric": fm["title"], "value": rows[0].get(fm["tfield"]),
                   "as_of": rows[0].get("record_date"), "source": "US Treasury FiscalData"}
            if fm.get("filter") and ":eq:" in fm["filter"]:
                rec["series"] = fm["filter"].split(":eq:")[-1]
            out(rec)

        if fm.get("search"):                                     # grants/awards search endpoints
            s = fm["search"]
            term = p.get("q") or p.get("term") or p.get("org") or p.get("topic")
            if not term:
                out({"error": f"need q= (a {s.get('want', 'search')} term)"}, 1)
            res = driver.accessor(identifier, s["operation"], **{s["arg"]: term})
            for part in s["extract"].split("."):
                res = res[int(part)] if isinstance(res, list) else res.get(part, [])
            out({"query": term, "source": fm.get("title"),
                 "results": res[:8] if isinstance(res, list) else res})
    except SystemExit as e:
        out({"error": str(e)}, 1)
    out({"error": "this leaf has no known structured retrieval shape"}, 1)


CMDS = {"sources": lambda a: cmd_sources(), "examples": lambda a: cmd_examples(),
        "find": cmd_find, "resolve": cmd_resolve, "fetch": cmd_fetch}

USAGE = """ard.py — deterministic toolbelt for the data-query skill (no LLM tokens; `find` calls the Agent Finder)

usage: ard.py <command> [args]

commands:
  sources                      list the data sources and what each covers
  examples                     example questions grouped by source
  find "<text>" [dir,dir]      rank OKF leaves for <text>, optionally scoped to source dirs
  resolve "<name>"             canonical ids/keys (CIK / EIN / FIPS) + place granularity for an entity
  fetch <identifier> [k=v ...] fetch the datum from one leaf
                                 keys: cik= | ticker= | ein= | geo= | place= | q= | period=FY2023
  -h, --help                   show this help

Every command prints JSON on stdout; on failure it prints {"error": ...} and exits non-zero."""

HELP = {"-h", "--help", "help", "-help", "--h", "?"}

if __name__ == "__main__":
    try:
        if len(sys.argv) < 2 or sys.argv[1] in HELP:
            print(USAGE)
            sys.exit(0)
        if sys.argv[1] not in CMDS:
            out({"error": f"unknown command {sys.argv[1]!r}. try: ard.py --help"}, 2)
        CMDS[sys.argv[1]](sys.argv[2:])
    except _Done as d:
        print(json.dumps(d.obj, indent=2, ensure_ascii=False))
        sys.exit(d.code)
