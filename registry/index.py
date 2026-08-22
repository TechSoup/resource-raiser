#!/usr/bin/env python3
"""Minimal in-memory ARD registry over OKF entries.

`build`  — embed every OKF doc that carries `representativeQueries` and cache the
           vectors + metadata.
`search` — embed an NL query and return the top-k matching entries (ARD-style:
           identifier, title, score, plus the bits the accessor needs).

Semantic matching uses the configured embedding provider (Azure OpenAI / OpenAI / Gemini,
or any OpenAI-compatible local host like Ollama via llm.py), so building the index needs an
embedding key. The /search shape mirrors ARD so this is swappable for a full registry later.
Set ARD_RERANK=0 to skip the second-stage LLM re-rank (much faster on slow/local models; the
embedding prefilter alone is usually enough).
"""
import os, sys, glob, json, hashlib
import numpy as np
import yaml
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
import llm            # provider-agnostic embeddings (Azure OpenAI | OpenAI | Gemini)

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SOURCES = os.path.join(ROOT, "sources")
CACHE_VEC = os.path.join(os.path.dirname(__file__), "vectors.npy")
CACHE_META = os.path.join(os.path.dirname(__file__), "meta.json")


def embed(texts, batch=96):
    return np.asarray(llm.embed(texts, batch), dtype=np.float32)


def frontmatter(path):
    t = open(path, encoding="utf-8").read()
    if not t.startswith("---"):
        return None
    _, fm, _b = t.split("---", 2)
    return yaml.safe_load(fm) or {}


_SCOPE_CACHE = {}


def scope_of(path, fm):
    """The SUBJECT SCOPE of a leaf — the kind of entity its source describes — taken from the
    source's `_access.md` `entityType`.

    Leaves are otherwise scope-blind, and that is what makes near-duplicate measures unstable:
    "Total revenue for the year" reads identically for a nonprofit's Form 990 and a public
    company's 10-K, so the two embed within 0.001 of each other and discovery picks one on a
    coin flip. The scope text is already authored per source (the classifier uses it to choose
    sources); this puts it in front of the embedding and the re-ranker too."""
    src = fm.get("source")
    if not (path and src):
        return ""
    ap = os.path.normpath(os.path.join(os.path.dirname(path), src))
    if ap not in _SCOPE_CACHE:
        try:
            _SCOPE_CACHE[ap] = ((frontmatter(ap) or {}).get("entityType") or "").strip()
        except Exception:
            _SCOPE_CACHE[ap] = ""
    return _SCOPE_CACHE[ap]


def index_text(fm, path=None):
    """The text a leaf is embedded as: title, the questions people ask for it, its subject
    scope, and its FULL definition. The description is no longer truncated — the tail of a
    definition is where a concept's exclusions live, and those are exactly what separate it
    from its siblings."""
    rq = fm.get("representativeQueries", []) or []
    scope = scope_of(path, fm)
    parts = [fm.get("title", "")] + rq
    if scope:
        parts.append(f"Describes {scope}")
    parts.append(fm.get("description", "") or "")
    return ". ".join(p for p in parts if p)


def normed(v):
    return v / np.clip(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9, None)


SEED = ("revenue", "income", "asset", "liabilit", "equity", "profit", "expense", "debt",
        "poverty", "population", "insurance", "rent", "employ", "diabetes")  # hard-case anchors for sampling


def build(batch=96, limit=None):
    """Embed every OKF leaf and cache vectors + metadata, writing incrementally so an
    interrupted build can resume. Each doc carries a `sig` (hash of its embedded text);
    on restart we reuse the longest saved prefix whose docs still match, and embed only
    the rest. A changed leaf (even just its representativeQueries) changes its sig, so a
    stale prefix is detected and rebuilt rather than silently reused.

    limit=N builds a smaller test index of N leaves — every leaf whose title hits a SEED
    anchor (so the hard disambiguation cases are kept) plus a stride sample of the rest."""
    emodel = llm.embed_model()                         # sig includes the model, so switching embedding
    docs, texts = [], []                               # providers (e.g. Gemini 3072-dim -> Ollama 768-dim)
    for path in sorted(glob.glob(os.path.join(SOURCES, "**", "*.md"), recursive=True)):  # invalidates the cache
        fm = frontmatter(path)
        if not fm or not fm.get("representativeQueries"):
            continue                                   # skip _access docs / non-entries
        text = index_text(fm, path)
        docs.append({
            "identifier": os.path.relpath(path, ROOT),
            "title": fm.get("title", ""),
            "scope": scope_of(path, fm),
            "description": (fm.get("description") or "")[:600],
            "concept": fm.get("concept"),
            "source": fm.get("source"),
            "queries": (fm.get("representativeQueries") or [])[:6],
            "sig": hashlib.md5((text + "|" + emodel).encode("utf-8")).hexdigest()[:12],
        })
        texts.append(text)

    if limit and len(docs) > limit:
        # test fixtures: always include the specific leaves whose disambiguation we test
        must = [i for i, d in enumerate(docs) if any(s in d["identifier"] for s in (
            "revenue-from-contract-with-customer-excluding-assessed-tax", "/revenues.md", "/profit-loss.md",
            "/assets.md", "/liabilities.md", "dp03-0062e", "dp03-0128e", "diabetes"))]
        anchor = [i for i, d in enumerate(docs) if any(s in d["title"].lower() for s in SEED) and i not in set(must)]
        rest = [i for i in range(len(docs)) if i not in set(must) | set(anchor)]
        pick = set(must)                               # must-haves always survive truncation
        for pool in (anchor, rest):                    # then stride-fill from hard cases, then the rest
            need = limit - len(pick)
            if need > 0 and pool:
                pick |= set(pool[::max(1, len(pool) // need)][:need])
        pick = sorted(pick)[:limit]
        docs, texts = [docs[i] for i in pick], [texts[i] for i in pick]
        srcs = {}
        for d in docs:
            srcs[_srcdir(d["identifier"])] = srcs.get(_srcdir(d["identifier"]), 0) + 1
        print(f"test build: {len(docs)} leaves across sources {srcs}")

    # reuse any previously-embedded vector whose (identifier, sig) is unchanged; embed ONLY
    # new or modified leaves. Keyed by content hash, not position, so inserting or editing a few
    # leaves is cheap no matter where they sort — no full re-embed when a source lands mid-list.
    reuse = {}
    if os.path.exists(CACHE_VEC) and os.path.exists(CACHE_META):
        prev, prev_vecs = json.load(open(CACHE_META)), np.load(CACHE_VEC)
        for d, v in zip(prev, prev_vecs):
            reuse[(d["identifier"], d["sig"])] = v
    vecs, todo = [None] * len(docs), []
    for i, d in enumerate(docs):
        hit = reuse.get((d["identifier"], d["sig"]))
        if hit is None:
            todo.append(i)
        else:
            vecs[i] = hit
    print(f"reusing {len(docs) - len(todo)} cached, embedding {len(todo)} new/changed of {len(docs)}…")
    def _persist(subset):
        done = [(d, v) for d, v in zip(docs, vecs) if v is not None] if subset else list(zip(docs, vecs))
        np.save(CACHE_VEC, np.asarray([v for _, v in done], dtype=np.float32))
        json.dump([d for d, _ in done], open(CACHE_META, "w"))

    for j in range(0, len(todo), batch):
        idx = todo[j:j + batch]
        embs = normed(embed([texts[i] for i in idx]))
        for k, i in enumerate(idx):
            vecs[i] = embs[k]
        print(f"  embedded {min(j + batch, len(todo))}/{len(todo)}")
        _persist(subset=True)                          # checkpoint each batch: an interrupt resumes here
    _persist(subset=False)
    print(f"indexed {len(docs)} entries -> {CACHE_VEC}")


def _card(i, c):
    """What the re-ranker sees for one candidate: what the table is, whose data it covers, and how
    people ask for it.

    NOT the description. The description exists to make the EMBEDDING discriminate — that is where
    a long definition earns its cost, and the prefilter has already used it by the time we get
    here. Repeating it to the re-ranker doubles the prompt (838 -> 429 chars per card, and a
    re-rank sends 60 of them) to restate what the title, scope and example queries already say.
    Set ARD_RERANK_DESC=1 to put it back."""
    s = f"{i}. {c['title']}"
    if c.get("scope"):
        s += f"\n   covers: {c['scope']}"
    if c.get("description") and os.getenv("ARD_RERANK_DESC", "0").lower() in ("1", "true", "yes"):
        s += f"\n   about: {c['description']}"
    if c.get("queries"):
        s += "\n   people ask: " + " | ".join(c["queries"][:6])
    return s


def _rerank(query, candidates, k):
    """Stage 2: a small LM scores the embedding candidates by actual relevance, seeing the
    full card for each (title, description, and the questions people ask for it)."""
    sys.path.insert(0, ROOT)                                   # driver.py lives at the project root
    import driver
    listing = "\n".join(_card(i, c) for i, c in enumerate(candidates))
    try:
        ranked = json.loads(driver.ask_llm(
            "You rank candidate data tables by how well each ANSWERS the user's query. Judge each "
            "candidate by its full card (title, description, and the example questions people ask for "
            "it): match the table's SUBJECT and SCOPE (the kind of entity, organization, or place it "
            "covers) and its measure to what the question is about. "
            "IMPORTANT: this stage is for RECALL, not final selection. KEEP every genuinely relevant "
            "table, INCLUDING close variants, siblings, and alternative definitions of the same measure "
            "(e.g. keep both a legacy and a current revenue concept; keep basic AND diluted EPS). Do NOT "
            "drop a candidate merely because another looks more 'headline' — the final choice is made "
            "downstream from the actual reported data, and a dropped candidate can never be chosen. "
            "For an amount prefer a dollar/count/median value; for a rate or share prefer a percentage. "
            f'Return JSON {{"ranked":[{{"i":<candidate number>,"score":<0-100 relevance>}}]}} '
            f"for the {k} most relevant tables, best first. Omit only the CLEARLY irrelevant.",
            f"Query: {query}\n\nCandidate tables:\n{listing}", json_mode=True,
            model=llm.rerank_model())).get("ranked", [])
    except Exception:
        return []
    out = []
    for r in ranked[:k]:
        i = r.get("i")
        if isinstance(i, int) and 0 <= i < len(candidates):
            out.append({**candidates[i], "score": r.get("score")})
    return out


_STORE = None
def _store():
    global _STORE
    if _STORE is None:                                       # load the index once, keep in memory
        _STORE = (np.load(CACHE_VEC), json.load(open(CACHE_META)))
    return _STORE


def _srcdir(identifier):
    parts = identifier.split("/")
    return parts[1] if len(parts) > 2 else None


# How many embedding hits get handed to the LLM re-rank. The single biggest lever on discovery
# cost, since the re-rank prompt is prefilter x one candidate card.
#
# Measured on the 193-case routing corpus (tests/route_eval.py), smaller is BOTH cheaper and more
# accurate — it is not a cost/quality trade-off:
#
#   prefilter   top-1   top-3   $/question
#      60       91.2%   93.3%   $0.00096
#      40       92.2%   93.3%   $0.00080
#      25       92.2%   93.8%   $0.00061
#      15       93.8%   93.8%   $0.00051
#   no re-rank  89.1%   93.3%   $0.00000
#
# Handing the re-ranker 60 candidates gives it 45 more chances to prefer a plausible sibling over
# the right table; the embedding prefilter, now that leaves carry full descriptions, is the better
# judge of which tables are even in contention. The re-rank still earns its keep (+4.7pt over none)
# — it just wants a short list. Raise it if a source's leaves are so alike that the embedding
# cannot separate them.
PREFILTER = int(os.getenv("ARD_PREFILTER", "15"))


def search(query, k=5, prefilter=None, sources=None, rerank=True):
    """Two-stage retrieval: embedding cosine prefilter (optionally scoped to given
    source directories — pass 2 of the entity->source, attribute->field design),
    then small-LM re-rank of the attribute against the fields in scope."""
    if os.getenv("ARD_RERANK", "1").lower() in ("0", "false", "no"):
        rerank = False                                 # embedding-only (fast on slow/local models)
    prefilter = prefilter or PREFILTER
    vecs, meta = _store()
    q = normed(embed([query]))[0]
    scores = vecs @ q
    cand = []
    for i in np.argsort(-scores):
        m = meta[i]
        if sources and _srcdir(m["identifier"]) not in sources:
            continue
        cand.append({**m, "embed_score": round(float(scores[i]) * 100, 1)})
        if len(cand) >= prefilter:
            break
    if not rerank:                                            # embedding-only: the caller ranks by data
        return [{**c, "score": c["embed_score"]} for c in cand[:k]]
    reranked = _rerank(query, cand, k)
    if reranked:
        return reranked
    return [{**c, "score": c["embed_score"]} for c in cand[:k]]


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build(limit=int(sys.argv[2]) if len(sys.argv) >= 3 else None)  # index.py build [limit]
    elif len(sys.argv) >= 3 and sys.argv[1] == "search":
        for r in search(" ".join(sys.argv[2:])):
            print(f'{r["score"]:5.1f}  {r["identifier"]}  ({r["title"]})')
    else:
        raise SystemExit("usage: index.py build | index.py search <query>")
