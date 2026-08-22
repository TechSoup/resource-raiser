#!/usr/bin/env python3
"""Data-driven DETAILED descriptions for OKF leaves.

Sibling of repr_queries.py, and the same bargain: given each leaf's (label, short
definition), a small LM writes the full paragraph that the one-line definition leaves
implicit — what the measure counts, what it excludes, and how it differs from the
sibling measures it is easy to confuse with.

Why this exists: a leaf is discovered by embedding `title + representativeQueries +
description`. A one-clause description ("Total revenue for the year.") carries almost no
signal, so two measures that mean genuinely different things — a nonprofit's Form 990
total revenue and a public company's us-gaap Revenues — land within 0.001 cosine of each
other and discovery picks between them on a coin flip. Detail is what separates them.

Grounding rule: the model expands what it is GIVEN. It is told not to invent form line
numbers, statutory citations, dates, or figures, because a confident wrong citation in a
descriptor is worse than a thin one — it would be embedded, re-ranked on, and shown.

Batched and cached to descriptions_cache.json, so a rebuild is cheap and stable.
"""
import os, sys, json
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
import driver                                                   # reuse the provider-agnostic ask_llm

CACHE = os.path.join(os.path.dirname(__file__), "descriptions_cache.json")
# The definition each description was expanded FROM. Kept as a side-car because most generated
# leaves are gitignored, so there is no committed "before" to diff against — without this, any
# later verification would be checking the description against nothing.
INPUTS = os.path.join(os.path.dirname(__file__), "descriptions_input.json")
_cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
_inputs = json.load(open(INPUTS)) if os.path.exists(INPUTS) else {}

PROMPT = (
    "Each line below is one {domain}: a key, then '::' then its label, then '::' then its short "
    "definition. These describe data about: {scope}\n\n"
    "For each, write a DETAILED description of 2-4 sentences (roughly 40-90 words) for a data "
    "catalog. It must make clear:\n"
    "  1. what the measure actually counts or reports, in plain language;\n"
    "  2. WHAT KIND OF SUBJECT it describes — restate the subject scope above explicitly, in "
    "words, because the description is read on its own without it;\n"
    "  3. what distinguishes it from broader, narrower, or sibling measures with similar names. "
    "State an EXCLUSION only when the label or definition actually states or clearly implies one "
    "('excluding assessed tax', 'among adults'). If nothing supports an exclusion, say nothing "
    "about exclusions — an invented 'focuses solely on X, excluding Y' is a false scope "
    "restriction, and those are the most common way this task goes wrong;\n"
    "  4. the unit and the reporting grain (per organization per fiscal year, per county, "
    "per period, dollars, percent, count) when they are evident.\n\n"
    "HARD RULES — a confidently wrong descriptor is worse than a thin one:\n"
    "  - Expand ONLY what the label and definition already say, plus the subject scope above. "
    "Do NOT invent form line numbers, schedule or part references, statutory citations, dates, "
    "agency names, thresholds, or any numeric value.\n"
    "  - Do NOT name specific organizations, companies, or places as examples.\n"
    "  - Carry hedges across FAITHFULLY. If the scope says 'mostly', 'typically', or 'largely', "
    "the description may not harden it into 'only', 'specific to', or 'all' — a sharpened hedge "
    "reads as a scope restriction that is simply false.\n"
    "  - Do NOT contradict the given definition or restate the label as a whole sentence.\n"
    "  - When you restate the subject scope (point 2), state it PLAINLY — 'pertains to publicly "
    "traded companies', never 'pertains SOLELY/EXCLUSIVELY/ONLY to'. The absolute adds nothing and "
    "turns a description of the subject into a claim about what the measure excludes.\n"
    "  - Plain prose, no markdown, no bullet points, no leading label.\n\n"
    'Return JSON {{"items":[{{"i":<line number>,"description":"..."}}]}}.'
)


def _flush_cache():
    """Merge-then-write, because two generators may be writing this cache at once.

    A plain `json.dump(_cache)` writes what THIS process loaded at import plus what it has
    added since — silently dropping everything a sibling generator wrote in the meantime.
    That costs only re-generation, but re-generation is the expensive part, so read the file
    back and let existing entries win before writing."""
    disk = {}
    if os.path.exists(CACHE):
        try:
            disk = json.load(open(CACHE))
        except (ValueError, OSError):
            disk = {}
    disk.update({k: v for k, v in _cache.items() if v})
    _cache.update(disk)
    tmp = CACHE + ".tmp"
    json.dump(_cache, open(tmp, "w"))
    os.replace(tmp, CACHE)                                      # atomic: never a half-written cache

    idisk = {}
    if os.path.exists(INPUTS):
        try:
            idisk = json.load(open(INPUTS))
        except (ValueError, OSError):
            idisk = {}
    idisk.update({k: v for k, v in _inputs.items() if v})
    _inputs.update(idisk)
    itmp = INPUTS + ".tmp"
    json.dump(_inputs, open(itmp, "w"))
    os.replace(itmp, INPUTS)


def scope_for(source_dir):
    """The subject scope a source covers, read from its `_access.md` `entityType` — the same
    text the classifier routes on, so a leaf's description and its source's routing can never
    disagree about what the source is about."""
    import yaml
    p = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "sources",
                                      source_dir, "_access.md"))
    try:
        t = open(p, encoding="utf-8").read()
        return ((yaml.safe_load(t.split("---", 2)[1]) or {}).get("entityType") or "").strip()
    except Exception:
        return ""


def for_items(items, domain, scope, batch=20, on_ready=None, workers=None):
    """items: list of (key, label, short_definition). domain: short phrase naming the corpus.
    scope: the subject the source covers (its `entityType`). Returns {key: description}.

    on_ready(key, description) fires the moment a description is known — from cache or freshly
    written — so callers write each leaf incrementally and survive interruption (the cache is
    flushed every batch).

    Batches run CONCURRENTLY (DESCRIPTIONS_WORKERS, default 8). One batch is a round trip of
    ~2k output tokens; run serially, the ~5.3k terse SEC concepts take hours, which is long
    enough that nobody regenerates them. on_ready still fires on the calling thread, so a
    generator's write_leaf never needs to be thread-safe."""
    from concurrent.futures import ThreadPoolExecutor
    workers = workers or int(os.getenv("DESCRIPTIONS_WORKERS", "8"))
    result, pending = {}, []

    def emit(k, d):
        result[k] = d
        if on_ready:
            on_ready(k, d)

    def run_batch(chunk):
        """One LLM round trip. Returns [(key, description)] — no shared state touched."""
        listing = "\n".join(f"{j}. {k} :: {lab} :: {(defn or '')[:400]}"
                            for j, (k, lab, defn) in enumerate(chunk))
        try:
            res = json.loads(driver.ask_llm(PROMPT.format(domain=domain, scope=scope),
                                            listing, json_mode=True)).get("items", [])
            got = {r["i"]: r.get("description", "") for r in res if isinstance(r.get("i"), int)}
        except Exception as e:
            # Falling back to the one-line definition is the safe outcome, but a SILENT fallback
            # looks exactly like success — the leaf is written, just uninformative. Say so, so a
            # rate-limited run is visible as degraded rather than passing as complete.
            print(f"  ! batch failed ({type(e).__name__}: {str(e)[:90]}) — "
                  f"{len(chunk)} leaves keep their short definition")
            got = {}
        out = []
        for j, (k, _lab, defn) in enumerate(chunk):
            d = got.get(j)
            d = d.strip() if isinstance(d, str) else ""
            out.append((k, d or (defn or "")))                  # fall back to the original definition
        return out

    for it in items:
        k, _lab, _defn = it[0], it[1], it[2]
        _inputs[k] = f"{_lab} :: {_defn or ''}"                 # the exact text the expander saw
    _flush_cache()

    for it in items:
        k = it[0]
        if k in _cache and _cache[k]:
            emit(k, _cache[k])                                  # already known — emit now
        else:
            pending.append(it)

    if pending:
        chunks = [pending[i:i + batch] for i in range(0, len(pending), batch)]
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for out in pool.map(run_batch, chunks):
                for k, d in out:
                    _cache[k] = d
                _flush_cache()                                  # checkpoint: an interrupt resumes here
                for k, d in out:
                    emit(k, d)
                done += len(out)
                print(f"  descriptions +{len(out)} ({done}/{len(pending)} new, {len(result)} total)")
    return result
