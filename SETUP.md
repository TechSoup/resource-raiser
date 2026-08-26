# Setup — choosing and configuring a model provider

Neural KG needs a **chat model** (classify, re-rank, acceptance-check, synthesize) and an
**embedding model** (build the ARD index, embed each query). It works with **OpenAI, Google Gemini,
Azure OpenAI, or a local OpenAI-compatible server (e.g. Ollama)** — all through one SDK (`llm.py`).

Pick one, put its variables in `set_keys.sh` (copy `set_keys.example.sh`), then `./run.sh`. The
provider is auto-detected from whichever key is set; you can force it with `LLM_PROVIDER`.

Rough cost of one question: **~4–6 chat calls** (classify → re-rank → acceptance-check → synthesize),
plus one embedding per query. Building the index is a one-time embedding pass over ~9k table
descriptions (or ~800 if you skip the SEC leaves — see below).

---

## 1. OpenAI

The simplest path. Paid, no surprises.

```bash
export OPENAI_API_KEY="sk-..."
# optional (these are the defaults):
export CHAT_MODEL="gpt-4o-mini"
export EMBED_MODEL="text-embedding-3-large"
```

---

## 2. Google Gemini

Works well, **but the free tier is too small to actually use the app** — plan on billing.

```bash
export LLM_PROVIDER=gemini
export GEMINI_API_KEY="..."                 # or GOOGLE_API_KEY
export CHAT_MODEL="gemini-2.0-flash"        # see note on model ids below
export EMBED_MODEL="gemini-embedding-001"   # NOT text-embedding-004 (that 404s on this endpoint)
```

- **Free-tier limits make it unusable for exploration.** `gemini-2.0-flash` free tier is roughly
  **5 requests/minute and 20 requests/day**. Since one question is ~4–6 chat calls, that's about
  **3–4 questions per day**, and the per-minute cap is one question's worth. Enable billing to
  actually use it.
- **Model ids drift.** Google retires ids and gates others to existing users. If a model returns
  `404 "no longer available to new users"` or `429`, list what your key can use and pick a current
  one:
  ```bash
  python3 -c "import llm; [print(m.id) for m in llm.client().models.list()]"
  ```
  At the time of writing, `gemini-embedding-001` (embeddings, 3072-dim) and a current flash chat
  model work; `text-embedding-004` and some `2.5` ids do not.
- **Batch-embed cap.** Gemini caps batch embedding at 100; the index build uses 96 and backs off on
  `429`, so the build is handled automatically (it's just slow on the free tier).

---

## 3. Azure OpenAI

Uses **deployment names**, not model ids.

```bash
export AZURE_OPENAI_API_KEY="..."
export AZURE_OPENAI_ENDPOINT="https://YOUR-RESOURCE.openai.azure.com"
export AZURE_OPENAI_API_VERSION="2024-08-01-preview"
export CHAT_DEPLOYMENT="your-gpt-4o-deployment"
export EMBED_DEPLOYMENT="your-embedding-deployment"
```

---

## 4. Local models (Ollama, or any OpenAI-compatible server)

No cost, no quota. Great for the **embedding build** even if you use a cloud chat model. Uses the
OpenAI code path pointed at a local base URL.

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY="ollama"               # any non-empty string; not used as a credential
export OPENAI_BASE_URL="http://localhost:11434/v1"
export CHAT_MODEL="llama3.1"                 # or any chat model you've pulled
export EMBED_MODEL="nomic-embed-text"        # fast local embeddings (768-dim)

# STRONGLY RECOMMENDED for local chat models:
export ARD_RERANK=0                          # skip the 2nd-stage LLM re-rank (see below)
export AGENT_FINDER_TIMEOUT=300              # give slow calls room before erroring
```

- **Set `ARD_RERANK=0`.** Discovery's second stage is an LLM re-rank over the candidate tables. On a
  small local model that single call can take **several minutes per query** — long enough to time
  out the whole request. The embedding prefilter alone selects the right table in ~0.1s and is
  usually enough, so turning the re-rank off makes local setups actually responsive.
- **Embeddings are the sweet spot.** `nomic-embed-text` embeds the ~800-leaf index in well under a
  minute, free — versus minutes and retries on a metered API. You can build the index locally and
  still point `CHAT_MODEL` at a cloud model if you prefer.
- **Chat on small local models is slow and JSON-flaky.** Expect ~40–70s per call and the occasional
  malformed classification (the engine degrades an unrecognized shape to a point lookup). Bigger
  local models are more reliable.

> **Switching embedding providers?** The index cache is keyed on the embedding model, so changing
> `EMBED_MODEL` automatically triggers a rebuild — no manual cleanup, and no mixing of different
> vector dimensions.

---

## Trimming the index (optional)

The SEC EDGAR source is ~8,100 of the ~9,000 table descriptions, so most of the embedding cost is
SEC. If you don't need public-company financials, move those leaves aside before the build and the
index shrinks by ~91%:

```bash
mkdir -p /tmp/parked && mv sources/sec-edgar/*.md /tmp/parked/ 2>/dev/null; \
  mv /tmp/parked/_access.md sources/sec-edgar/          # keep the source description
python3 registry/index.py build                          # ~800 leaves instead of ~9,000
```

Regenerate them any time with `python3 tools/gen_sec_okf.py` (~30s).

---

## macOS + python.org Python

The generators fetch public taxonomies over `urllib`, which uses OpenSSL's own trust store — **not**
the macOS Keychain. On a python.org framework build that store may be empty, and the first `./run.sh`
fails with `CERTIFICATE_VERIFY_FAILED`. Fix it once with the installer's own script:

```bash
"/Applications/Python 3.xx/Install Certificates.command"
```

(Homebrew and pyenv Pythons are not affected.)
