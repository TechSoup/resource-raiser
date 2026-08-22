#!/usr/bin/env python3
"""End-to-end ARD data demo (SEC slice).

  natural-language question
    -> ARD discovery (registry/index.py)   : which concept ("table")?
    -> LLM extracts company + period
    -> resolve ticker -> CIK (SEC)
    -> generic accessor (accessor/okf_fetch.py) : fetch the live data
    -> LLM synthesizes a cited answer

Run:  source the Azure keys, then  python3 driver.py "how much did Apple spend on R&D in 2023?"
"""
import os, re, sys, json, time, subprocess, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import yaml
import llm            # provider-agnostic chat/embeddings (Azure OpenAI | OpenAI | Gemini)
import ard_client

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "ard-data-demo (guha@guha.com)"


class CredentialError(Exception):
    """A source cannot answer because a required credential is missing (an API key, a GCP project).
    Distinct from the SystemExit/Backtrack a normal 'this source has no data' miss raises: no amount
    of backtracking over other hits/entities/periods can satisfy it, so the search must STOP and tell
    the user to set the key — not spend ~2 minutes exhausting every other option first."""

# In-process cache of SEC companyconcept responses, keyed by (cik, concept). fetch_metric probes
# ~25 candidate concepts per query and the harness may re-enter it while backtracking; caching makes
# repeats free. None = the company doesn't report that concept (a 404), also worth remembering.
_SEC_CONCEPT_CACHE = {}
_SEC_SEARCH_CACHE = {}      # the concept-candidate search per metric_query — identical across backtracks
_METRIC_CACHE = {}          # whole fetch_metric result (or failure) per (metric, cik, period)


def _sec_concept(cik, concept):
    """One SEC companyconcept, fetched IN-PROCESS (no subprocess) and cached. Returns the JSON, or
    None if the company doesn't report it. Retries 429/5xx so rate-limiting isn't misread as 'not
    reported' (which would silently drop the right concept)."""
    key = (str(int(cik)), concept)
    if key in _SEC_CONCEPT_CACHE:
        return _SEC_CONCEPT_CACHE[key]
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{int(cik):0>10}/us-gaap/{concept}.json"
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                        timeout=20) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break                                         # not reported -> None
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(0.4 * (attempt + 1)); continue
            break
        except Exception:
            if attempt < 2:
                time.sleep(0.3); continue
            break
    _SEC_CONCEPT_CACHE[key] = data
    return data


def ask_llm(system, user, json_mode=False, model=None, stage="other"):
    """One chat turn via the configured provider. Built lazily inside llm.client(), so the
    deterministic tools (fetch/resolve/accessor) import driver without needing any LLM keys.
    `model` selects a non-default model for this call (e.g. the ranking model)."""
    return llm.chat(system, user, json_mode, model=model, stage=stage)


def frontmatter(rel):
    t = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    return yaml.safe_load(t.split("---", 2)[1])


_TMAP = None
def ticker_to_cik(ticker):
    global _TMAP
    if _TMAP is None:
        req = urllib.request.Request("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": UA})
        _TMAP = {v["ticker"].upper(): (str(v["cik_str"]), v["title"]) for v in json.load(urllib.request.urlopen(req, timeout=30)).values()}
    return _TMAP.get(ticker.upper(), (None, None))


def accessor(rel, op, **params):
    cmd = [sys.executable, os.path.join(ROOT, "accessor", "okf_fetch.py"), os.path.join(ROOT, rel), op]
    cmd += [f"{k}={v}" for k, v in params.items()]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        # A missing-credential failure is not a backtrack-able miss (see CredentialError). The accessor
        # marks it with a stable prefix so we can classify it across the subprocess boundary.
        if "CREDENTIAL_ERROR:" in (out.stderr or ""):
            raise CredentialError(out.stderr.split("CREDENTIAL_ERROR:", 1)[1].strip().splitlines()[0])
        raise SystemExit(f"accessor error: {out.stderr}")
    return json.loads(out.stdout)


def _days(u):
    from datetime import date
    a, b = (date(*map(int, x.split("-"))) for x in (u["start"], u["end"]))
    return (b - a).days


def _select_unit(units, family):
    """Pick the (unit_key, rows) from an XBRL `units` dict matching the concept's unit family.
    The response is keyed by unit — USD, "USD/shares", shares, pure — so a per-share or share-count
    concept is read from the right key instead of assuming USD. Returns ('', []) if absent so the
    caller backtracks to another concept."""
    if not units:
        return "", []
    def has(pred):
        return next(((k, v) for k, v in units.items() if pred(k)), ("", []))
    if family == "per-share":
        return has(lambda k: "/" in k)                        # e.g. USD/shares, EUR/shares
    if family == "shares":
        return has(lambda k: k.lower() == "shares")
    if family in ("percent", "pure"):
        return has(lambda k: k.lower() in ("pure", "percent"))
    # currency: prefer USD, else any 3-letter currency code the filer reports in
    if "USD" in units:
        return "USD", units["USD"]
    return has(lambda k: len(k) == 3 and k.isalpha() and "/" not in k)


def pick_value(units, period, ptype, strict=False):
    """Select the annual figure by filing dates (robust to non-December fiscal years).
    With a specific year requested and `strict`, return None if that year is absent
    (so the caller can reject this concept and try the next candidate)."""
    rows = [u for u in units if u.get("form") in ("10-K", "20-F")]
    if ptype != "instant":                                   # keep ~full-year durations
        rows = [u for u in rows if "start" in u and 350 <= _days(u) <= 380] or rows
    rows = rows or units
    yr = re.sub(r"\D", "", period or "")
    if len(yr) == 4:                                          # a specific fiscal year
        m = [u for u in rows if u["end"][:4] == yr]
        if m:
            return max(m, key=lambda u: u["end"])
        if strict:
            return None
    return max(rows, key=lambda u: u["end"])                  # latest annual


def fetch_metric(metric_query, ticker=None, period="latest", k=25, log=True, cik=None):
    """Discover the right SEC concept for `metric_query`, then return the value the
    company actually reports. Tries the top-k discovered concepts and picks the
    first one with data (fixes obscure-variant mis-ranking + non-reported concepts)."""
    if cik:                                                   # canonical key supplied
        title = ticker or f"CIK {cik}"
    else:
        cik, title = ticker_to_cik(ticker)
        if not cik:
            raise SystemExit(f"no CIK for ticker {ticker}")

    mk = (metric_query, str(int(cik)), period or "latest")   # memoize: the harness re-enters this with
    if mk in _METRIC_CACHE:                                   # identical args on every backtrack attempt
        v = _METRIC_CACHE[mk]
        if isinstance(v, str):
            raise SystemExit(v)                              # cached failure (e.g. no reportable data)
        return v

    def try_hit(hit):
        fm = frontmatter(hit["identifier"])
        if not fm.get("concept"):
            return None                                       # non-SEC entry
        data = _sec_concept(cik, fm["concept"])               # in-process + cached
        if not data:
            return None                                       # company doesn't report it (404)
        unit, rows = _select_unit(data.get("units", {}), fm.get("unit", "currency"))
        if not rows:
            return None                                       # not reported in the expected unit
        row = pick_value(rows, period, fm.get("periodType", "duration"), strict=True)
        if row is None:
            return None                                       # reports the concept but not this period
        if log:
            print(f"  • {metric_query!r} → {fm['concept']} ({title}) FY{row['end'][:4]} = {row['val']:,} {unit}")
        src = frontmatter(os.path.join(os.path.dirname(hit["identifier"]), fm["source"])) if fm.get("source") else fm
        did = (src.get("trust") or {}).get("identity", src.get("resource"))
        out = {"company": data["entityName"], "metric": fm["title"].split(" — ")[0],
               "concept": f"us-gaap:{fm['concept']}", "period": f"FY{row['end'][:4]}",
               "period_end": row["end"], "value": row["val"], "unit": unit, "source": f"SEC EDGAR ({did})"}
        if unit != "shares" and "/" not in unit and unit != "pure":
            out["value_usd"] = row["val"]                     # back-compat for currency amounts
        return out

    # Attribute is already entity-expunged + scoped to SEC. The LLM reranker is unreliable here:
    # among ~8,500 near-synonym concepts it favours a literal name match ("Revenues") and DROPS the
    # correct ASC-606 concept (RevenueFromContractWithCustomerExcludingAssessedTax) as a "narrow
    # variant" — so it never reaches the fetch stage. The fix is to select from the REPORTED DATA,
    # not from names: pull a wide EMBEDDING pool (bypassing the reranker), fetch what the company
    # actually files for each, and choose using each candidate's real latest year + value.
    pool = max(k, 50)                                        # wide pool so the right concept is present
    skey = (metric_query, pool)
    hits = _SEC_SEARCH_CACHE.get(skey)
    if hits is None:                                         # cache the finder call: identical on every backtrack
        hits = ard_client.search(metric_query, k=pool, sources=["sec-edgar"], rerank=False)
        _SEC_SEARCH_CACHE[skey] = hits
    # Probe the candidate concepts CONCURRENTLY (each is one cached SEC call). Sequentially this was
    # ~25 subprocess+HTTP round-trips (~14s) and dominated latency; in parallel it's a couple seconds.
    # Modest fan-out keeps us under SEC's ~10 req/s ceiling; the cache absorbs the rest.
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(try_hit, hits))
    reported = [(rank, r) for rank, r in enumerate(results) if r]
    if not reported:
        _METRIC_CACHE[mk] = msg = f"no reportable data for {metric_query!r} / {title}"
        raise SystemExit(msg)

    want_year = re.sub(r"\D", "", period or "")
    if len(want_year) == 4:                                   # a specific year is pinned; keep only those
        yr_hits = [rr for rr in reported if rr[1]["period"][2:] == want_year] or reported
        _METRIC_CACHE[mk] = out = _pick_by_data(metric_query, yr_hits, log)
        return out
    _METRIC_CACHE[mk] = out = _pick_by_data(metric_query, reported, log)
    return out


def _pick_by_data(metric_query, reported, log=True):
    """Choose the concept that truly answers `metric_query`, judging by the DATA each candidate
    reports (its latest year and value), not by name similarity. A concept the company has stopped
    filing is a discontinued alias; among current concepts the LLM matches the specific measure
    (total vs a sub-component, diluted vs basic) using the magnitudes it can see."""
    # De-duplicate by concept, keeping each concept's freshest record.
    by_concept = {}
    for rank, r in reported:
        c = r["concept"]
        if c not in by_concept or r["period_end"] > by_concept[c][1]["period_end"]:
            by_concept[c] = (rank, r)
    cands = sorted(by_concept.values(), key=lambda rr: rr[0])
    if len(cands) == 1:
        return cands[0][1]

    listing = "\n".join(
        f'{i}. {r["concept"]}  (latest {r["period"]}, value {r["value"]:,} {r["unit"]})'
        for i, (_rank, r) in enumerate(cands))
    try:
        pick = json.loads(ask_llm(
            "Pick the ONE us-gaap concept that best answers the MEASURE, judging by the reported data. "
            "Rules: (1) A concept last filed years before the newest candidate is a DISCONTINUED alias — "
            "do not pick it when a current concept reports the same measure. (2) Match the SPECIFIC "
            "measure: for a 'total'/overall figure prefer the largest current concept in that family; "
            "for a named variant (e.g. diluted vs basic EPS) pick that exact one, not the largest. "
            'Return JSON {"i": <index>}.\nMEASURE: ' + metric_query + "\nCANDIDATES:\n" + listing,
            metric_query, json_mode=True, stage="resolve-concept")).get("i")
    except Exception:
        pick = None
    if not isinstance(pick, int) or not (0 <= pick < len(cands)):
        # fail safe: freshest, then best rank — never the stale literal-name match
        pick = max(range(len(cands)), key=lambda i: (cands[i][1]["period_end"], -cands[i][0]))
    best = cands[pick][1]
    if log:
        print(f"  → picked {best['concept']} {best['period']} = {best['value']:,} {best['unit']} "
              f"(chosen from {len(cands)} reported concepts by data)")
    return best


def main(question):
    info = json.loads(ask_llm(
        "Extract the company stock ticker and fiscal period from the question. "
        'Respond JSON: {"ticker": "<TICKER or empty>", "period": "FY<year> or latest"}.',
        question, json_mode=True, stage="resolve-entity"))
    if not info.get("ticker"):
        raise SystemExit("could not identify a company in the question")
    r = fetch_metric(question, info["ticker"], info.get("period", "latest"))
    print("\n" + ask_llm(
        "Answer the user's question in one sentence using ONLY the data provided. Cite the source.",
        json.dumps({"question": question, **r})))


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "How much did Apple spend on R&D in 2023?")
