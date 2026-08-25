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
import asyncio, os, re, sys, json, time, threading, urllib.request, urllib.error
from collections import OrderedDict
import httpx
import llm            # provider-agnostic chat/embeddings (Azure OpenAI | OpenAI | Gemini)
import ard_client
import runtime

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "ard-data-demo (guha@guha.com)"


class CredentialError(Exception):
    """A source cannot answer because a required credential is missing (an API key, a GCP project).
    Distinct from the SystemExit/Backtrack a normal 'this source has no data' miss raises: no amount
    of backtracking over other hits/entities/periods can satisfy it, so the search must STOP and tell
    the user to set the key — not spend ~2 minutes exhausting every other option first."""


class SourceRateLimitError(RuntimeError):
    """A publisher is temporarily throttling requests; callers should report and retry later."""

# One SEC `companyfacts` payload contains every us-gaap concept a company reports. Cache that
# payload by CIK, then expose the old per-concept lookup interface to the selection code below.
# This turns a 50-candidate measure search from ~50 SEC requests into one.
_SEC_COMPANYFACTS_CACHE = OrderedDict()
_SEC_COMPANYFACTS_CACHE_SIZE = int(os.getenv("SEC_COMPANYFACTS_CACHE_SIZE", "16"))
_SEC_SEARCH_CACHE = {}      # the concept-candidate search per metric_query — identical across backtracks
_METRIC_CACHE = {}          # whole fetch_metric result (or failure) per (metric, cik, period)
_SEC_CONCEPT_META = None
# This bounds one process. The deployed service intentionally runs one Python worker; a future
# multi-worker or multi-VM deployment needs a shared limiter rather than silently relying on this.
_SEC_REQUEST_LOCK = threading.Lock()
_SEC_NEXT_REQUEST = 0.0
_SEC_COMPANYFACTS_LOCK = threading.Lock()


def _pace_sec_request():
    """Keep all SEC requests in this process below eight requests/second."""
    global _SEC_NEXT_REQUEST
    with _SEC_REQUEST_LOCK:
        now = time.monotonic()
        if now < _SEC_NEXT_REQUEST:
            time.sleep(_SEC_NEXT_REQUEST - now)
        _SEC_NEXT_REQUEST = time.monotonic() + 0.125


def _sec_companyfacts(cik):
    """Fetch all XBRL facts for one company once; cache only success or a definitive 404."""
    key = str(int(cik))
    # Several ambiguity branches can ask about the same company concurrently. One lock makes the
    # first branch fetch while the others wait for its cache entry instead of issuing duplicates.
    with _SEC_COMPANYFACTS_LOCK:
        if key in _SEC_COMPANYFACTS_CACHE:
            data = _SEC_COMPANYFACTS_CACHE.pop(key)
            _SEC_COMPANYFACTS_CACHE[key] = data               # bounded LRU: this company is hot
            return data
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):0>10}.json"
        data, absent, last_error = None, False, None
        for attempt in range(5):
            try:
                _pace_sec_request()
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                            timeout=30) as response:
                    data = json.load(response)
                break
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    absent = True
                    break
                last_error = error
                if error.code in (429, 500, 502, 503) and attempt < 4:
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = min(8.0, 0.75 * (2 ** attempt))
                    time.sleep(delay)
                    continue
                if error.code == 429:
                    raise SourceRateLimitError(
                        "SEC is temporarily rate limiting requests; please try again shortly") from error
                raise
            except Exception as error:
                last_error = error
                if attempt < 4:
                    time.sleep(min(4.0, 0.5 * (2 ** attempt)))
                    continue
                raise
        if data is None and not absent:
            raise RuntimeError(f"SEC companyfacts request failed for CIK {key}: {last_error}")
        _SEC_COMPANYFACTS_CACHE[key] = data
        while len(_SEC_COMPANYFACTS_CACHE) > max(1, _SEC_COMPANYFACTS_CACHE_SIZE):
            _SEC_COMPANYFACTS_CACHE.popitem(last=False)
        return data


def _sec_concept(cik, concept):
    """Return one us-gaap concept from the company's cached `companyfacts` payload."""
    company = _sec_companyfacts(cik)
    fact = (((company or {}).get("facts") or {}).get("us-gaap") or {}).get(concept)
    return ({**fact, "entityName": company.get("entityName", "")}
            if isinstance(fact, dict) else None)


class AsyncSecClient:
    """Application-owned SEC client with one event-loop token bucket and de-duplicated CIK fetches."""
    def __init__(self, http_client, requests_per_second=8):
        self.http = http_client
        self.interval = 1 / requests_per_second
        self.next_request = 0.0
        self.pace_lock = asyncio.Lock()
        self.cache_lock = asyncio.Lock()
        self.companyfacts = OrderedDict()
        self.cik_locks = {}
        self.ticker_lock = asyncio.Lock()
        self.tickers = None

    async def _pace(self, context):
        await context.wait(self.pace_lock.acquire())
        try:
            delay = self.next_request - time.monotonic()
            if delay > 0:
                await context.sleep(delay)
            self.next_request = time.monotonic() + self.interval
        finally:
            self.pace_lock.release()

    async def _json(self, url, context, attempts=5):
        for attempt in range(attempts):
            await self._pace(context)
            try:
                response = await context.wait(self.http.get(
                    url, headers={"User-Agent": UA}, timeout=min(30, context.remaining() or 30)))
            except (asyncio.CancelledError, runtime.QueryCancelled):
                raise
            except httpx.RequestError:
                if attempt == attempts - 1:
                    raise
                await context.sleep(min(4.0, 0.5 * (2 ** attempt)))
                continue
            if response.status_code == 404:
                return None
            if response.status_code in (429, 500, 502, 503) and attempt < attempts - 1:
                try:
                    delay = float(response.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    delay = min(8.0, 0.75 * (2 ** attempt))
                await context.sleep(delay)
                continue
            if response.status_code == 429:
                raise SourceRateLimitError(
                    "SEC is temporarily rate limiting requests; please try again shortly")
            response.raise_for_status()
            return response.json()
        raise RuntimeError("SEC request failed")

    async def company_facts(self, cik, context):
        key = str(int(cik))
        async with self.cache_lock:
            if key in self.companyfacts:
                value = self.companyfacts.pop(key)
                self.companyfacts[key] = value
                return value
            lock = self.cik_locks.setdefault(key, asyncio.Lock())
        await context.wait(lock.acquire())
        try:
            async with self.cache_lock:
                if key in self.companyfacts:
                    value = self.companyfacts.pop(key)
                    self.companyfacts[key] = value
                    return value
            url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):0>10}.json"
            value = await self._json(url, context)
            async with self.cache_lock:
                self.companyfacts[key] = value
                while len(self.companyfacts) > max(1, _SEC_COMPANYFACTS_CACHE_SIZE):
                    self.companyfacts.popitem(last=False)
            return value
        finally:
            lock.release()

    async def ticker_to_cik(self, ticker, context):
        if self.tickers is None:
            await context.wait(self.ticker_lock.acquire())
            try:
                if self.tickers is None:
                    data = await self._json("https://www.sec.gov/files/company_tickers.json", context)
                    self.tickers = {item["ticker"].upper(): (str(item["cik_str"]), item["title"])
                                    for item in data.values()}
            finally:
                self.ticker_lock.release()
        return self.tickers.get(ticker.upper(), (None, None))

    async def concept(self, cik, concept, context):
        company = await self.company_facts(cik, context)
        fact = (((company or {}).get("facts") or {}).get("us-gaap") or {}).get(concept)
        return ({**fact, "entityName": company.get("entityName", "")}
                if isinstance(fact, dict) else None)


def ask_llm(system, user, json_mode=False, model=None, stage="other", max_tokens=None,
            reasoning_effort=None):
    """One chat turn via the configured provider. Built lazily inside llm.client(), so the
    deterministic tools (fetch/resolve/accessor) import driver without needing any LLM keys.
    `model` selects a non-default model for this call (e.g. the ranking model)."""
    return llm.chat(system, user, json_mode, model=model, stage=stage, max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort)


def frontmatter(rel):
    from accessor import okf_fetch
    path = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
    return okf_fetch.load_okf(os.path.normpath(path))


_TMAP = None
def ticker_to_cik(ticker):
    global _TMAP
    if _TMAP is None:
        _pace_sec_request()                                  # one cached SEC request, still paced
        req = urllib.request.Request("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": UA})
        _TMAP = {v["ticker"].upper(): (str(v["cik_str"]), v["title"]) for v in json.load(urllib.request.urlopen(req, timeout=30)).values()}
    return _TMAP.get(ticker.upper(), (None, None))


def accessor(rel, op, **params):
    """In-process compatibility path; the request-time accessor subprocess is gone."""
    from accessor import okf_fetch
    try:
        return okf_fetch.fetch(os.path.join(ROOT, rel), op, params)
    except SystemExit as exc:
        message = str(exc)
        if "CREDENTIAL_ERROR:" in message:
            raise CredentialError(message.split("CREDENTIAL_ERROR:", 1)[1].strip().splitlines()[0])
        raise


async def accessor_async(rel, op, *, context, **params):
    from accessor import okf_fetch
    try:
        return await okf_fetch.fetch_async(os.path.join(ROOT, rel), op, params, context=context)
    except okf_fetch.PublisherRateLimitError as exc:
        raise SourceRateLimitError(str(exc)) from exc
    except SystemExit as exc:
        message = str(exc)
        if "CREDENTIAL_ERROR:" in message:
            raise CredentialError(message.split("CREDENTIAL_ERROR:", 1)[1].strip().splitlines()[0])
        raise


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


def _concept_meta(concept):
    """Resolve a chosen us-gaap concept to its indexed descriptor without trusting a file path
    supplied by a caller."""
    global _SEC_CONCEPT_META
    if _SEC_CONCEPT_META is None:
        _SEC_CONCEPT_META = {}
        try:
            from registry import index
            with open(index.CACHE_META) as f:
                for item in json.load(f):
                    if item.get("concept") and item.get("identifier", "").startswith("sources/sec-edgar/"):
                        _SEC_CONCEPT_META.setdefault(item["concept"], item)
        except Exception:
            pass
    return _SEC_CONCEPT_META.get(str(concept or "").removeprefix("us-gaap:"))


def fetch_metric(metric_query, ticker=None, period="latest", k=25, log=True, cik=None,
                 concept=None):
    """Discover the right SEC concept for `metric_query`, then return the value the
    company actually reports. Tries the top-k discovered concepts and picks the
    first one with data (fixes obscure-variant mis-ranking + non-reported concepts)."""
    if cik:                                                   # canonical key supplied
        title = ticker or f"CIK {cik}"
    else:
        cik, title = ticker_to_cik(ticker)
        if not cik:
            raise SystemExit(f"no CIK for ticker {ticker}")

    forced_concept = str(concept or "").removeprefix("us-gaap:") or None
    mk = (metric_query, str(int(cik)), period or "latest", forced_concept)
                                                               # memoize: the harness re-enters this with
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

    # A clarification choice is an explicit user constraint. Fetch that exact indexed concept;
    # do not run semantic selection again and risk asking the same question in a loop.
    if forced_concept:
        chosen_hit = _concept_meta(forced_concept)
        if not chosen_hit:
            _METRIC_CACHE[mk] = msg = f"unknown SEC concept choice {forced_concept!r}"
            raise SystemExit(msg)
        chosen = try_hit(chosen_hit)
        if not chosen:
            _METRIC_CACHE[mk] = msg = (f"{title} does not report {forced_concept} for "
                                        f"{period or 'latest'}")
            raise SystemExit(msg)
        _METRIC_CACHE[mk] = chosen
        return chosen

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
        # Broad headline concepts must not depend on whether an embedding search over thousands of
        # near-synonymous XBRL leaves happens to put them in its top 50. Add their indexed leaves to
        # the probe pool; the normal reported-data chooser below still verifies that the company
        # actually files them.
        for canonical in _canonical_sec_concepts(metric_query):
            meta = _concept_meta(canonical)
            if meta and not any(h.get("identifier") == meta.get("identifier") for h in hits):
                hits.insert(0, meta)
        _SEC_SEARCH_CACHE[skey] = hits
    # `companyfacts` has already made every reported concept a local dictionary lookup, so there is
    # no network fan-out to parallelize here.
    results = [try_hit(hit) for hit in hits]
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


async def fetch_metric_async(metric_query, ticker=None, period="latest", k=25, log=True, cik=None,
                             concept=None, *, context):
    """Async SEC companyfacts path; one paced request supplies every candidate concept."""
    sec = context.sec_client
    if sec is None:
        if context.http_client is None:
            raise RuntimeError("async SEC access requires QueryContext.http_client")
        sec = context.sec_client = AsyncSecClient(context.http_client)
    if cik:
        title = ticker or f"CIK {cik}"
    else:
        cik, title = await sec.ticker_to_cik(ticker, context)
        if not cik:
            raise SystemExit(f"no CIK for ticker {ticker}")
    company = await sec.company_facts(cik, context)

    def try_hit(hit):
        metadata = frontmatter(hit["identifier"])
        concept_name = metadata.get("concept")
        fact = (((company or {}).get("facts") or {}).get("us-gaap") or {}).get(concept_name)
        if not concept_name or not isinstance(fact, dict):
            return None
        data = {**fact, "entityName": company.get("entityName", "")}
        unit, rows = _select_unit(data.get("units", {}), metadata.get("unit", "currency"))
        if not rows:
            return None
        row = pick_value(rows, period, metadata.get("periodType", "duration"), strict=True)
        if row is None:
            return None
        source = (frontmatter(os.path.join(os.path.dirname(hit["identifier"]), metadata["source"]))
                  if metadata.get("source") else metadata)
        identity = (source.get("trust") or {}).get("identity", source.get("resource"))
        result = {"company": data["entityName"], "metric": metadata["title"].split(" — ")[0],
                  "concept": f"us-gaap:{concept_name}", "period": f"FY{row['end'][:4]}",
                  "period_end": row["end"], "value": row["val"], "unit": unit,
                  "source": f"SEC EDGAR ({identity})"}
        if unit != "shares" and "/" not in unit and unit != "pure":
            result["value_usd"] = row["val"]
        return result

    forced = str(concept or "").removeprefix("us-gaap:") or None
    if forced:
        metadata = _concept_meta(forced)
        result = try_hit(metadata) if metadata else None
        if not result:
            raise SystemExit(f"{title} does not report {forced} for {period or 'latest'}")
        return result
    pool = max(k, 50)
    hits = await ard_client.search_async(
        metric_query, k=pool, sources=["sec-edgar"], rerank=False, context=context)
    for canonical in _canonical_sec_concepts(metric_query):
        metadata = _concept_meta(canonical)
        if metadata and not any(hit.get("identifier") == metadata.get("identifier") for hit in hits):
            hits.insert(0, metadata)
    reported = [(rank, result) for rank, result in
                enumerate(try_hit(hit) for hit in hits) if result]
    if not reported:
        raise SystemExit(f"no reportable data for {metric_query!r} / {title}")
    year = re.sub(r"\D", "", period or "")
    if len(year) == 4:
        reported = [item for item in reported if item[1]["period"][2:] == year] or reported
    return await _pick_by_data_async(metric_query, reported, context, log)


def _canonical_sec_concepts(metric_query):
    normalized = " ".join(re.findall(r"[a-z0-9]+", str(metric_query).lower()))
    families = {
        "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                    "SalesRevenueNet"),
        "revenues": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                     "SalesRevenueNet"),
        "total revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                          "SalesRevenueNet"),
        "assets": ("Assets",),
        "total assets": ("Assets",),
        "net income": ("NetIncomeLoss",),
    }
    return families.get(normalized, ())


def _canonical_sec_index(metric_query, candidates):
    """Return the canonical headline concept for a deliberately small set of broad SEC measures.

    This is not a cross-source ontology. It is a source-specific safety rule for XBRL families where
    a broad user term otherwise collides with specialized siblings that contain the same word. Add a
    family only after observing a concrete collision and only when the taxonomy has a clear headline
    concept.
    """
    preferred = _canonical_sec_concepts(metric_query)
    if not preferred:
        return None
    by_concept = {str(row.get("concept") or "").removeprefix("us-gaap:"): i
                  for i, (_rank, row) in enumerate(candidates)}
    return next((by_concept[concept] for concept in preferred if concept in by_concept), None)


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
    resolution = {}
    try:
        parsed = json.loads(ask_llm(
            "Pick the ONE us-gaap concept that best answers the MEASURE, judging by the reported data. "
            "Rules: (1) A concept last filed years before the newest candidate is a DISCONTINUED alias — "
            "do not pick it when a current concept reports the same measure. (2) Match the SPECIFIC "
            "measure: for a 'total'/overall figure prefer the largest current concept in that family; "
            "for a named variant (e.g. diluted vs basic EPS) pick that exact one, not the largest. "
            "Also identify material ambiguity among CURRENT concepts: alternatives are genuinely "
            "different readings a user could reasonably mean, not merely nearby taxonomy terms. "
            "Set dominant=true only when the wording clearly selects one reading. If it does not, "
            "include the selected index and 1-3 other plausible indices in alternatives. "
            "Same-value aliases are not ambiguity. "
            'Return JSON {"i": <index>, "dominant": true|false, "alternatives": [<indices>], '
            '"why": "<short reason>"}.\nMEASURE: ' + metric_query + "\nCANDIDATES:\n" + listing,
            metric_query, json_mode=True, stage="resolve-concept"))
        resolution = parsed if isinstance(parsed, dict) else {}
        pick = resolution.get("i")
    except Exception:
        pick = None
    return _finish_sec_pick(metric_query, cands, resolution, pick, log)


def _finish_sec_pick(metric_query, cands, resolution, pick, log):
    canonical = _canonical_sec_index(metric_query, cands)
    if canonical is not None and isinstance(pick, int) and 0 <= pick < len(cands) and pick != canonical:
        # The semantic resolver selected a specialized sibling for a broad measure. Make the
        # disagreement observable: prefer the headline concept for non-interactive clients, and
        # retain the model's selected, fetched value as a clarification option for interactive ones.
        resolution = {**resolution, "i": canonical, "dominant": False,
                      "alternatives": [canonical, pick] + list(resolution.get("alternatives") or []),
                      "why": ("a broad SEC measure matched both the headline concept and a "
                              "specialized sibling")}
        pick = canonical
    elif canonical is not None and not isinstance(pick, int):
        pick = canonical
    if not isinstance(pick, int) or not (0 <= pick < len(cands)):
        # fail safe: freshest, then best rank — never the stale literal-name match
        pick = max(range(len(cands)), key=lambda i: (cands[i][1]["period_end"], -cands[i][0]))
    best = dict(cands[pick][1])
    if resolution.get("dominant") is False:
        raw_indices = [pick] + list(resolution.get("alternatives") or [])
        indices = list(dict.fromkeys(i for i in raw_indices
                                     if isinstance(i, int) and 0 <= i < len(cands)))[:4]
        selected_value = best.get("value")

        def materially_different(candidate):
            value = candidate.get("value")
            try:
                scale = max(abs(float(value)), abs(float(selected_value)), 1.0)
                return abs(float(value) - float(selected_value)) / scale >= 0.05
            except (TypeError, ValueError):
                return value != selected_value

        alternatives = [dict(cands[i][1]) for i in indices]
        # Asking about aliases that report the same number creates noise rather than clarity.
        if any(materially_different(candidate) for candidate in alternatives[1:]):
            best["_ambiguity"] = {
                "attribute": metric_query,
                "reason": str(resolution.get("why") or "multiple reported concepts remain plausible"),
                "options": alternatives,
            }
    if log:
        print(f"  → picked {best['concept']} {best['period']} = {best['value']:,} {best['unit']} "
              f"(chosen from {len(cands)} reported concepts by data)")
    return best


async def _pick_by_data_async(metric_query, reported, context, log=True):
    by_concept = {}
    for rank, result in reported:
        concept = result["concept"]
        if concept not in by_concept or result["period_end"] > by_concept[concept][1]["period_end"]:
            by_concept[concept] = (rank, result)
    candidates = sorted(by_concept.values(), key=lambda item: item[0])
    if len(candidates) == 1:
        return candidates[0][1]
    listing = "\n".join(
        f'{index}. {result["concept"]}  (latest {result["period"]}, '
        f'value {result["value"]:,} {result["unit"]})'
        for index, (_rank, result) in enumerate(candidates))
    resolution, pick = {}, None
    try:
        answer = await llm.chat_async(
            "Pick the ONE us-gaap concept that best answers the MEASURE, judging by the reported data. "
            "Rules: (1) A concept last filed years before the newest candidate is a DISCONTINUED alias — "
            "do not pick it when a current concept reports the same measure. (2) Match the SPECIFIC "
            "measure: for a 'total'/overall figure prefer the largest current concept in that family; "
            "for a named variant pick that exact one, not the largest. Identify material ambiguity among "
            "CURRENT concepts. Set dominant=true only when wording clearly selects one reading. "
            'Return JSON {"i":<index>,"dominant":true|false,"alternatives":[<indices>],"why":"..."}.\n'
            "MEASURE: " + metric_query + "\nCANDIDATES:\n" + listing,
            metric_query, context=context, json_mode=True, stage="resolve-concept")
        parsed = json.loads(answer)
        resolution = parsed if isinstance(parsed, dict) else {}
        pick = resolution.get("i")
    except (asyncio.CancelledError, runtime.QueryCancelled):
        raise
    except Exception:
        pass
    return _finish_sec_pick(metric_query, candidates, resolution, pick, log)


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
