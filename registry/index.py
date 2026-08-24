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
import os, sys, glob, json, hashlib, shutil
from datetime import datetime, timezone
import numpy as np
import yaml
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
import llm            # provider-agnostic embeddings (Azure OpenAI | OpenAI | Gemini)

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SOURCES = os.path.join(ROOT, "sources")
REGISTRY = os.path.dirname(__file__)
BUILDS = os.path.join(REGISTRY, "builds")
CURRENT = os.path.join(REGISTRY, "current")
LEGACY_VEC = os.path.join(REGISTRY, "vectors.npy")
LEGACY_META = os.path.join(REGISTRY, "meta.json")


def _active_paths():
    """One immutable index generation. Legacy files remain readable during migration."""
    if os.path.isdir(CURRENT):
        return os.path.join(CURRENT, "vectors.npy"), os.path.join(CURRENT, "meta.json")
    return LEGACY_VEC, LEGACY_META


CACHE_VEC, CACHE_META = _active_paths()


def embed(texts, batch=96):
    return np.asarray(llm.embed(texts, batch), dtype=np.float32)


def frontmatter(path):
    with open(path, encoding="utf-8") as f:
        t = f.read()
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

    corpus_hash = hashlib.sha256(json.dumps(
        {"entries": [(d["identifier"], d["sig"]) for d in docs], "model": emodel},
        separators=(",", ":"), sort_keys=True).encode()).hexdigest()[:20]
    os.makedirs(BUILDS, exist_ok=True)
    final_dir = os.path.join(BUILDS, corpus_hash)
    stage_dir = os.path.join(BUILDS, f".{corpus_hash}.staging")
    os.makedirs(stage_dir, exist_ok=True)
    stage_vec, stage_meta = (os.path.join(stage_dir, "vectors.npy"),
                             os.path.join(stage_dir, "meta.json"))

    # reuse any previously-embedded vector whose (identifier, sig) is unchanged; embed ONLY
    # new or modified leaves. Keyed by content hash, not position, so inserting or editing a few
    # leaves is cheap no matter where they sort — no full re-embed when a source lands mid-list.
    reuse = {}
    for prev_vec, prev_meta in ((stage_vec, stage_meta), _active_paths()):
        if os.path.exists(prev_vec) and os.path.exists(prev_meta):
            with open(prev_meta) as f:
                prev = json.load(f)
            prev_vecs = np.load(prev_vec)
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
        vtmp, mtmp = stage_vec + ".tmp", stage_meta + ".tmp"
        with open(vtmp, "wb") as f:
            np.save(f, np.asarray([v for _, v in done], dtype=np.float32))
        with open(mtmp, "w") as f:
            json.dump([d for d, _ in done], f)
        os.replace(vtmp, stage_vec)
        os.replace(mtmp, stage_meta)

    for j in range(0, len(todo), batch):
        idx = todo[j:j + batch]
        embs = normed(embed([texts[i] for i in idx]))
        for k, i in enumerate(idx):
            vecs[i] = embs[k]
        print(f"  embedded {min(j + batch, len(todo))}/{len(todo)}")
        _persist(subset=True)                          # checkpoint each batch: an interrupt resumes here
    _persist(subset=False)
    vecs_check = np.load(stage_vec)
    with open(stage_meta) as f:
        meta_check = json.load(f)
    if len(vecs_check) != len(meta_check) or len(meta_check) != len(docs) or len(vecs_check.shape) != 2:
        raise SystemExit("index validation failed: vector/metadata count or dimensions differ")
    manifest = {
        "corpus_hash": corpus_hash, "embedding_provider": llm.provider(),
        "embedding_model": emodel, "vector_dimension": int(vecs_check.shape[1]),
        "entry_count": len(meta_check), "generator_commit": _git_commit(),
        "prompt_versions": _prompt_versions(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(stage_dir, "manifest.json.tmp"), "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(os.path.join(stage_dir, "manifest.json.tmp"), os.path.join(stage_dir, "manifest.json"))
    if not os.path.exists(final_dir):
        os.replace(stage_dir, final_dir)
    else:
        shutil.rmtree(stage_dir)
    link_tmp = CURRENT + ".tmp"
    try:
        os.unlink(link_tmp)
    except FileNotFoundError:
        pass
    os.symlink(os.path.relpath(final_dir, os.path.dirname(CURRENT)), link_tmp)
    os.replace(link_tmp, CURRENT)
    global CACHE_VEC, CACHE_META, _STORE
    CACHE_VEC, CACHE_META = os.path.join(CURRENT, "vectors.npy"), os.path.join(CURRENT, "meta.json")
    _STORE = None
    # A successful publication supersedes checkpoints from failed/incompatible generations. Keep
    # the current staging directory during a failed build so it can resume; clean all of them only
    # after another generation has been fully validated and selected.
    for stale in glob.glob(os.path.join(BUILDS, ".*.staging")):
        if os.path.isdir(stale):
            shutil.rmtree(stale)
    print(f"indexed {len(docs)} entries -> generation {corpus_hash}")


def _git_commit():
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def _prompt_versions():
    """Content hashes distinguish generation runs even when prompts changed before a commit."""
    out = {}
    for name in ("descriptions.py", "repr_queries.py"):
        path = os.path.join(ROOT, "tools", name)
        try:
            with open(path, "rb") as f:
                out[name] = hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            out[name] = "missing"
    return out


def verify():
    """Validate the active generation as a single artifact set without calling a provider."""
    vec, meta = _active_paths()
    try:
        v = np.load(vec)
        with open(meta) as f:
            m = json.load(f)
        manifest_path = os.path.join(CURRENT, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path) as f:
                manifest = json.load(f)
        else:
            manifest = {}
        ok = len(v.shape) == 2 and len(v) == len(m) and len(m) > 0
        if manifest:
            ok = ok and manifest.get("entry_count") == len(m) and manifest.get("vector_dimension") == v.shape[1]
        return ok, {"entries": len(m), "dimensions": int(v.shape[1]), **manifest}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


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


MIN_RERANK_SCORE = float(os.getenv("ARD_MIN_RERANK_SCORE", "50"))


class RelevanceScoringError(RuntimeError):
    """The LLM relevance stage failed; embedding neighbors must not escape as answers."""


class NoRelevantTablesError(LookupError):
    def __init__(self, top_score=None, threshold=MIN_RERANK_SCORE):
        self.top_score = top_score
        self.threshold = threshold
        super().__init__("no table cleared the LLM relevance threshold")


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
            f"for the {k} most relevant tables, best first. Omit only the CLEARLY irrelevant. "
            "Return only the compact JSON object; do not explain any choice.",
            f"Query: {query}\n\nCandidate tables:\n{listing}", json_mode=True,
            model=llm.rerank_model(),
            max_tokens=int(os.getenv("ARD_RERANK_MAX_TOKENS", "400")),
            reasoning_effort=os.getenv("ARD_RERANK_REASONING_EFFORT", "low"))).get("ranked", [])
    except Exception:
        return None
    if not isinstance(ranked, list):
        return None
    scored = []
    for r in ranked[:k]:
        if not isinstance(r, dict):
            continue
        i = r.get("i")
        score = r.get("score")
        if (isinstance(i, int) and 0 <= i < len(candidates)
                and isinstance(score, (int, float))):
            scored.append({**candidates[i], "score": score})
    eligible = [candidate for candidate in scored if candidate["score"] >= MIN_RERANK_SCORE]
    if candidates and not eligible:
        top = max((candidate["score"] for candidate in scored), default=None)
        raise NoRelevantTablesError(top)
    return eligible


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


def search_many(queries, k=5, prefilter=None, sources=None, rerank=True, rerank_query=None):
    """Retrieve several phrasings in one embedding call and rerank their union once.

    The aggregate is max similarity, not an averaged vector: a table surfaced decisively by either
    the entity-expunged attribute or the original question must remain eligible. One union rerank is
    the cost-saving boundary; independently reranking each phrasing repeats the expensive prompt.
    """
    if os.getenv("ARD_RERANK", "1").lower() in ("0", "false", "no"):
        rerank = False                                 # embedding-only (fast on slow/local models)
    queries = list(dict.fromkeys(str(q).strip() for q in queries if str(q).strip()))
    if not queries:
        return []
    # `k` is a caller contract. The default rerank pool is deliberately small, but an embedding-only
    # caller (SEC resolution asks for 50 reported concepts) must not be silently truncated to the
    # 15-entry rerank prefilter before slicing to k.
    prefilter = max(prefilter or PREFILTER, k)
    vecs, meta = _store()
    q = normed(embed(queries))
    scores = np.max(vecs @ q.T, axis=1)
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
    reranked = _rerank(rerank_query or queries[0], cand, k)
    if reranked is None:
        raise RelevanceScoringError("LLM table relevance scoring failed")
    return reranked


def search(query, k=5, prefilter=None, sources=None, rerank=True):
    """Two-stage retrieval for one query; see search_many for the shared implementation."""
    return search_many([query], k=k, prefilter=prefilter, sources=sources, rerank=rerank,
                       rerank_query=query)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build(limit=int(sys.argv[2]) if len(sys.argv) >= 3 else None)  # index.py build [limit]
    elif len(sys.argv) >= 3 and sys.argv[1] == "search":
        for r in search(" ".join(sys.argv[2:])):
            print(f'{r["score"]:5.1f}  {r["identifier"]}  ({r["title"]})')
    elif len(sys.argv) >= 2 and sys.argv[1] == "verify":
        ok, detail = verify()
        print(json.dumps(detail, indent=2))
        raise SystemExit(0 if ok else 1)
    else:
        raise SystemExit("usage: index.py build | index.py search <query>")
