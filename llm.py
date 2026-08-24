#!/usr/bin/env python3
"""Provider-agnostic chat + embeddings. Works with Azure OpenAI, OpenAI, OpenRouter, or Gemini — all through
the OpenAI SDK (Gemini via its OpenAI-compatible endpoint), so the rest of the codebase calls one
interface regardless of provider.

Pick a provider with LLM_PROVIDER, or leave it unset and it auto-detects from whichever key is present:

  Azure   AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_VERSION,
          CHAT_DEPLOYMENT, EMBED_DEPLOYMENT                      # Azure uses *deployment* names
  OpenAI  OPENAI_API_KEY,  CHAT_MODEL (default gpt-4o-mini),  EMBED_MODEL (default text-embedding-3-large)
  Gemini  GEMINI_API_KEY (or GOOGLE_API_KEY),  CHAT_MODEL (default gemini-2.0-flash),
          EMBED_MODEL (default text-embedding-004)

See set_keys.example.sh for the full list.
"""
import os, time, threading
import runtime

_client = None
_provider = None

# Gemini model ids change often; these are current working defaults (verify with client().models.list()).
# text-embedding-004 does NOT exist on this endpoint (404) — the embedding model is gemini-embedding-001.
_CHAT_DEFAULT = {"openai": "gpt-4o-mini", "gemini": "gemini-2.0-flash"}
_EMBED_DEFAULT = {"openai": "text-embedding-3-large", "gemini": "gemini-embedding-001"}
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def provider():
    """azure | openai | gemini — from LLM_PROVIDER, else auto-detected from the keys present."""
    global _provider
    if _provider is None:
        p = os.getenv("LLM_PROVIDER", "").strip().lower()
        if not p:
            if os.getenv("AZURE_OPENAI_API_KEY"):
                p = "azure"
            elif os.getenv("OPENROUTER_API_KEY"):
                p = "openrouter"
            elif os.getenv("OPENAI_API_KEY"):
                p = "openai"
            elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
                p = "gemini"
            else:
                raise SystemExit("No LLM credentials found. Set one provider's keys — "
                                 "AZURE_OPENAI_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY "
                                 "(see set_keys.example.sh).")
        _provider = p
    return _provider


def have_credentials():
    """True if ANY supported provider is configured — used by the servers to fail early with a
    clear, provider-agnostic message instead of assuming Azure."""
    return bool(os.getenv("LLM_PROVIDER") or os.getenv("AZURE_OPENAI_API_KEY")
                or os.getenv("OPENROUTER_API_KEY")
                or os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY")
                or os.getenv("GOOGLE_API_KEY"))


_NO_CREDS = ("No LLM credentials set. Configure ONE provider (OpenRouter, Azure OpenAI, OpenAI, or Gemini) — "
             "copy set_keys.example.sh to set_keys.sh and fill it in, or export the keys. See the README.")


# A stalled connection must not hang a build. The SDK's default timeout is long enough that one
# dropped embedding response blocks a 9k-leaf index build indefinitely with no output; a bounded
# timeout turns that into a retryable error, which embed()/chat() already handle.
_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))
_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))


def _build():
    p = provider()
    if p == "azure":
        from openai import AzureOpenAI
        return AzureOpenAI(api_key=os.environ["AZURE_OPENAI_API_KEY"],
                           azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                           api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
                           timeout=_TIMEOUT, max_retries=_RETRIES)
    from openai import OpenAI
    if p == "gemini":
        return OpenAI(api_key=os.getenv("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"],
                      base_url=os.getenv("OPENAI_BASE_URL", _GEMINI_BASE),
                      timeout=_TIMEOUT, max_retries=_RETRIES)
    if p == "openrouter":
        return OpenAI(api_key=os.environ["OPENROUTER_API_KEY"],
                      base_url=os.getenv("OPENROUTER_BASE_URL", _OPENROUTER_BASE),
                      default_headers={"HTTP-Referer": os.getenv("OPENROUTER_APP_URL", ""),
                                       "X-Title": os.getenv("OPENROUTER_APP_TITLE", "Resource Raiser")},
                      timeout=_TIMEOUT, max_retries=_RETRIES)
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"],       # plain OpenAI or an OpenAI-compatible host
                  base_url=os.getenv("OPENAI_BASE_URL") or None,
                  timeout=_TIMEOUT, max_retries=_RETRIES)


def client():
    global _client
    if _client is None:
        _client = _build()
    return _client


def chat_model():
    # Azure addresses a DEPLOYMENT name; OpenAI/Gemini address a model id.
    if provider() == "azure":
        return os.getenv("CHAT_DEPLOYMENT", "gpt-4o-mini")
    if provider() == "openrouter":
        return os.getenv("OPENROUTER_MODEL") or os.getenv("CHAT_MODEL", "openai/gpt-4o-mini")
    return os.getenv("CHAT_MODEL", _CHAT_DEFAULT[provider()])


def rerank_model():
    """The model that RANKS candidate tables. Split from chat_model() because the two stages want
    different things: ranking is the token-heavy call (a page of candidates, several times per
    question) and wants cheap and fast, while classification and synthesis are single calls where
    quality shows. Falls back to the chat model when unset."""
    return os.getenv("RERANK_MODEL") or chat_model()


def embed_model():
    if provider() == "azure":
        return os.getenv("EMBED_DEPLOYMENT", "text-embedding-3-large")
    if provider() == "openrouter":
        return os.getenv("OPENROUTER_EMBEDDING_MODEL") or os.getenv("EMBED_MODEL", "openai/text-embedding-3-small")
    return os.getenv("EMBED_MODEL", _EMBED_DEFAULT[provider()])



# --- usage accounting ---------------------------------------------------------------------------
# Every question costs several chat calls plus an embedding, and on a metered provider that is real
# money you cannot see. A Ledger is a per-question accumulator: the harness binds one for the
# request, and every chat()/embed() below adds to it — including calls made on fan-out worker
# threads, which is why the ledger is a SHARED object rather than a thread-local counter.
#
# Scope is THIS PROCESS only. The ARD Agent Finder is a separate service with its own lifecycle
# (and its own ledger, if it ever wants one); its embedding and re-rank are not billed to the
# caller's question.
_LEDGER = threading.local()

# USD per 1M tokens, (input, output). Matched by longest substring, so a provider-prefixed id like
# "openai/gpt-4o-mini" resolves the same as a bare one. Override per-run with LLM_PRICE_IN /
# LLM_PRICE_OUT / LLM_PRICE_EMBED. Prices drift — this is a fallback for providers that do not
# report cost; OpenRouter reports its own, and that is preferred whenever present.
_PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gemini-2.0-flash": (0.10, 0.40),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "gemini-embedding-001": (0.15, 0.0),
    "nomic-embed-text": (0.0, 0.0),                    # local (Ollama) — free
}


def price_for(model):
    """(input, output) USD per 1M tokens for a model id, or (0, 0) if unknown."""
    env_in, env_out = os.getenv("LLM_PRICE_IN"), os.getenv("LLM_PRICE_OUT")
    if env_in or env_out:
        return (float(env_in or 0), float(env_out or 0))
    m = (model or "").lower()
    hit = [k for k in _PRICES if k in m]
    return _PRICES[max(hit, key=len)] if hit else (0.0, 0.0)


class Ledger:
    """Counts LLM calls, tokens and cost for one unit of work (normally one question)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.chat_calls = 0
        self.embed_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.embed_tokens = 0
        self.cost_usd = 0.0
        self.cost_is_reported = False                  # True once a provider gave us a real figure
        self.by_model = {}
        self.by_stage = {}

    def record(self, kind, model, prompt_tokens=0, completion_tokens=0, reported_cost=None,
               stage="other"):
        pin, pout = price_for(model)
        cost = (reported_cost if reported_cost is not None
                else (prompt_tokens * pin + completion_tokens * pout) / 1e6)
        with self._lock:
            if kind == "chat":
                self.chat_calls += 1
                self.prompt_tokens += prompt_tokens
                self.completion_tokens += completion_tokens
            else:
                self.embed_calls += 1
                self.embed_tokens += prompt_tokens
            self.cost_usd += cost
            if reported_cost is not None:
                self.cost_is_reported = True
            for bucket, key in ((self.by_model, model), (self.by_stage, stage)):
                b = bucket.setdefault(key, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
                b["calls"] += 1
                b["tokens"] += prompt_tokens + completion_tokens
                b["cost_usd"] += cost

    def snapshot(self):
        with self._lock:
            return {
                "llm_calls": self.chat_calls + self.embed_calls,
                "chat_calls": self.chat_calls,
                "embed_calls": self.embed_calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "embed_tokens": self.embed_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens + self.embed_tokens,
                "cost_usd": round(self.cost_usd, 6),
                "cost_source": "provider" if self.cost_is_reported else "price-table",
                "by_model": {k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                             for k, v in self.by_model.items()},
                "by_stage": {k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                             for k, v in self.by_stage.items()},
            }


def start_ledger():
    """Begin accounting on this thread; returns the Ledger to bind onto worker threads too."""
    led = Ledger()
    _LEDGER.led = led
    return led


def bind_ledger(led):
    _LEDGER.led = led                                  # always assign: pool threads are reused


def ledger():
    return getattr(_LEDGER, "led", None)


def _record(kind, model, usage, reported_cost=None, stage="other"):
    led = ledger()
    if led is None or usage is None:
        return
    led.record(kind, model,
               getattr(usage, "prompt_tokens", 0) or 0,
               getattr(usage, "completion_tokens", 0) or 0,
               reported_cost, stage)


def _openrouter():
    return provider() == "openrouter" or "openrouter.ai" in (os.getenv("OPENAI_BASE_URL") or "")


def _reported_cost(usage):
    """OpenRouter returns the actual charge on `usage.cost` when asked for it. Anything else
    reports nothing, and the price table fills in."""
    if usage is None:
        return None
    c = getattr(usage, "cost", None)
    if c is None:
        c = (getattr(usage, "model_extra", None) or {}).get("cost")
    return float(c) if isinstance(c, (int, float)) else None


def chat(system, user, json_mode=False, model=None, stage="other", max_tokens=None,
         reasoning_effort=None):
    """One chat turn (system + user), temperature 0. json_mode asks for a JSON object back.
    `model` overrides the default chat model for one call (see rerank_model()).
    `stage` labels what the call was FOR — classify / resolve / check / synthesize — so a
    question's bill can be read by what it was spent on rather than as one lump. Output and
    reasoning limits are opt-in: short structural tasks such as reranking should not inherit the
    provider's unconstrained reasoning defaults."""
    runtime.check()
    kw = {"response_format": {"type": "json_object"}} if json_mode else {}
    if max_tokens is not None:
        kw["max_tokens"] = int(max_tokens)
    if _openrouter():
        extra = {"usage": {"include": True}}              # ask OpenRouter for the actual charge
        if reasoning_effort:
            extra["reasoning"] = {"effort": reasoning_effort}
        # Route by THROUGHPUT, not sticker price. A cheap model is often served by a single
        # provider whose rate limit a parallel fan-out hits immediately, turning a "cheaper" model
        # into stalls and 429s. Set LLM_PROVIDER_SORT="" to let OpenRouter choose.
        sort = os.getenv("LLM_PROVIDER_SORT", "throughput").strip()
        if sort:
            extra["provider"] = {"sort": sort}
        kw["extra_body"] = extra
    model = model or chat_model()
    r = client().chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
    u = getattr(r, "usage", None)
    _record("chat", model, u, _reported_cost(u), stage)
    return r.choices[0].message.content


def embed(texts, batch=96):
    """Embed a list of strings -> list of vectors. Robust to two provider quirks:
      - a 429 / quota / rate error backs OFF and retries the same chunk (it does NOT fan out into
        one call per string, which would burn a free-tier quota that's already exhausted);
      - a too-large batch (e.g. Gemini caps batch-embed at 100) is SPLIT and retried, not failed.
    Default batch 96 stays under Gemini's hard cap of 100."""
    runtime.check()
    c, model = client(), embed_model()

    def _call(chunk, depth=0):
        runtime.check()
        try:
            r = c.embeddings.create(model=model, input=chunk)
            u = getattr(r, "usage", None)
            _record("embed", model, u, _reported_cost(u))
            return [d.embedding for d in r.data]
        except Exception as e:
            m = str(e).lower()
            if ("429" in m or "quota" in m or "rate limit" in m or "resource_exhausted" in m
                    or "timeout" in m or "timed out" in m) and depth < 6:
                time.sleep(min(30, 2 ** depth))                 # back off, retry the SAME chunk
                return _call(chunk, depth + 1)
            if len(chunk) > 1:                                  # oversized batch or transient: split
                h = len(chunk) // 2
                return _call(chunk[:h], depth) + _call(chunk[h:], depth)
            raise

    out = []
    for i in range(0, len(texts), batch):
        out.extend(_call([t[:8000] for t in texts[i:i + batch]]))
        if len(texts) > batch:
            print(f"  embedded {min(i + batch, len(texts))}/{len(texts)}")
    return out
