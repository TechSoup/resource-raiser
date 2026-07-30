#!/usr/bin/env python3
"""Provider-agnostic chat + embeddings. Works with Azure OpenAI, OpenAI, or Gemini — all through
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
import os, time

_client = None
_provider = None

# Gemini model ids change often; these are current working defaults (verify with client().models.list()).
# text-embedding-004 does NOT exist on this endpoint (404) — the embedding model is gemini-embedding-001.
_CHAT_DEFAULT = {"openai": "gpt-4o-mini", "gemini": "gemini-2.0-flash"}
_EMBED_DEFAULT = {"openai": "text-embedding-3-large", "gemini": "gemini-embedding-001"}
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def provider():
    """azure | openai | gemini — from LLM_PROVIDER, else auto-detected from the keys present."""
    global _provider
    if _provider is None:
        p = os.getenv("LLM_PROVIDER", "").strip().lower()
        if not p:
            if os.getenv("AZURE_OPENAI_API_KEY"):
                p = "azure"
            elif os.getenv("OPENAI_API_KEY"):
                p = "openai"
            elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
                p = "gemini"
            else:
                raise SystemExit("No LLM credentials found. Set one provider's keys — "
                                 "AZURE_OPENAI_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY "
                                 "(see set_keys.example.sh).")
        _provider = p
    return _provider


def have_credentials():
    """True if ANY supported provider is configured — used by the servers to fail early with a
    clear, provider-agnostic message instead of assuming Azure."""
    return bool(os.getenv("LLM_PROVIDER") or os.getenv("AZURE_OPENAI_API_KEY")
                or os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY")
                or os.getenv("GOOGLE_API_KEY"))


_NO_CREDS = ("No LLM credentials set. Configure ONE provider (Azure OpenAI, OpenAI, or Gemini) — "
             "copy set_keys.example.sh to set_keys.sh and fill it in, or export the keys. See the README.")


def _build():
    p = provider()
    if p == "azure":
        from openai import AzureOpenAI
        return AzureOpenAI(api_key=os.environ["AZURE_OPENAI_API_KEY"],
                           azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                           api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"))
    from openai import OpenAI
    if p == "gemini":
        return OpenAI(api_key=os.getenv("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"],
                      base_url=os.getenv("OPENAI_BASE_URL", _GEMINI_BASE))
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"],       # plain OpenAI or an OpenAI-compatible host
                  base_url=os.getenv("OPENAI_BASE_URL") or None)


def client():
    global _client
    if _client is None:
        _client = _build()
    return _client


def chat_model():
    # Azure addresses a DEPLOYMENT name; OpenAI/Gemini address a model id.
    if provider() == "azure":
        return os.getenv("CHAT_DEPLOYMENT", "gpt-4o-mini")
    return os.getenv("CHAT_MODEL", _CHAT_DEFAULT[provider()])


def embed_model():
    if provider() == "azure":
        return os.getenv("EMBED_DEPLOYMENT", "text-embedding-3-large")
    return os.getenv("EMBED_MODEL", _EMBED_DEFAULT[provider()])


def chat(system, user, json_mode=False):
    """One chat turn (system + user), temperature 0. json_mode asks for a JSON object back."""
    kw = {"response_format": {"type": "json_object"}} if json_mode else {}
    r = client().chat.completions.create(
        model=chat_model(), temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
    return r.choices[0].message.content


def embed(texts, batch=96):
    """Embed a list of strings -> list of vectors. Robust to two provider quirks:
      - a 429 / quota / rate error backs OFF and retries the same chunk (it does NOT fan out into
        one call per string, which would burn a free-tier quota that's already exhausted);
      - a too-large batch (e.g. Gemini caps batch-embed at 100) is SPLIT and retried, not failed.
    Default batch 96 stays under Gemini's hard cap of 100."""
    c, model = client(), embed_model()

    def _call(chunk, depth=0):
        try:
            r = c.embeddings.create(model=model, input=chunk)
            return [d.embedding for d in r.data]
        except Exception as e:
            m = str(e).lower()
            if ("429" in m or "quota" in m or "rate limit" in m or "resource_exhausted" in m) and depth < 6:
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
