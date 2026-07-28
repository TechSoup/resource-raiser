#!/usr/bin/env python3
"""Data-driven representative queries for OKF leaves.

Replaces hand-curated alias tables: given each leaf's (label, definition), a small
LM writes the natural-language questions a person would ask to retrieve THAT
specific measure — using the definition to distinguish it from broader/legacy
variants. Uniform across every source; no per-concept curation.

Batched and cached to repr_queries_cache.json, so a rebuild is cheap and stable.
"""
import os, sys, json
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
import driver                                                   # reuse the Azure client + ask_llm

CACHE = os.path.join(os.path.dirname(__file__), "repr_queries_cache.json")
_cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}


def for_items(items, domain, batch=25, on_ready=None):
    """items: list of (key, label, definition). domain: short phrase naming the corpus
    (e.g. 'US-GAAP financial statement concept'). Returns {key: [queries]}.

    If on_ready(key, queries) is given it is called for each item the moment its queries
    are known — from the cache or freshly generated — so callers can write each leaf out
    incrementally and survive interruption (the cache is flushed every batch)."""
    result, pending = {}, []

    def emit(k, qs):
        result[k] = qs
        if on_ready:
            on_ready(k, qs)

    def flush():
        if not pending:
            return
        listing = "\n".join(f"{j}. {lab} :: {(defn or '')[:300]}" for j, (_, lab, defn) in enumerate(pending))
        prompt = (
            f"Each line below is one {domain}: a label, then '::', then its definition. "
            "For each, write 3-5 short natural-language questions or phrases a person would use to ask "
            "for THIS specific measure — everyday wording, common synonyms and abbreviations. Use the "
            "definition to make them specific enough to distinguish this measure from broader, legacy, or "
            "sibling variants. Do not invent facts or entity names. "
            'Return JSON {"items":[{"i":<line number>,"queries":["...","..."]}]}.')
        try:
            res = json.loads(driver.ask_llm(prompt, listing, json_mode=True)).get("items", [])
            got = {r["i"]: r.get("queries", []) for r in res if isinstance(r.get("i"), int)}
        except Exception:
            got = {}
        for j, (k, lab, _defn) in enumerate(pending):
            qs = [q for q in got.get(j, []) if isinstance(q, str) and q.strip()]
            _cache[k] = qs or [lab]                             # fall back to the label alone
        json.dump(_cache, open(CACHE, "w"))
        for k, _lab, _defn in pending:
            emit(k, _cache[k])
        print(f"  repr-queries +{len(pending)} ({len(result)} done)")
        pending.clear()

    for it in items:
        k = it[0]
        if k in _cache:
            emit(k, _cache[k])                                  # already known — write it out now
        else:
            pending.append(it)
            if len(pending) >= batch:
                flush()
    flush()
    return result
