#!/usr/bin/env bash
# Copy this file to set_keys.sh (which is gitignored) and fill in ONE provider's values.
# run.sh sources set_keys.sh automatically; or export these some other way.
#
# Per-provider setup, model ids, and gotchas (Gemini free-tier limits, local Ollama, etc.): see SETUP.md
#
# The model provider is auto-detected from whichever key you set. To force it, set:
#   export LLM_PROVIDER=azure   # or: openai | gemini

# ============================================================================
# Option A — Azure OpenAI  (uses *deployment* names, not model ids)
# ============================================================================
export AZURE_OPENAI_API_KEY="your-key-here"
export AZURE_OPENAI_ENDPOINT="https://YOUR-RESOURCE.openai.azure.com"
export AZURE_OPENAI_API_VERSION="2024-08-01-preview"
export CHAT_DEPLOYMENT="your-gpt-4o-deployment-name"        # a chat model deployment
export EMBED_DEPLOYMENT="your-embedding-deployment-name"    # a text-embedding-3-* deployment

# ============================================================================
# Option B — OpenAI
# ============================================================================
# export OPENAI_API_KEY="sk-..."
# export CHAT_MODEL="gpt-4o-mini"                # optional (this is the default)
# export EMBED_MODEL="text-embedding-3-large"    # optional (this is the default)
# export OPENAI_BASE_URL=""                      # optional: an OpenAI-compatible host

# ============================================================================
# Option C — Gemini  (via its OpenAI-compatible endpoint)
#   NOTE: the FREE tier is ~20 requests/day — about 3-4 questions. Enable billing to explore.
#   Model ids drift; if one 404s/429s, list yours (see SETUP.md). embed model is gemini-embedding-001.
# ============================================================================
# export LLM_PROVIDER=gemini
# export GEMINI_API_KEY="..."                    # or GOOGLE_API_KEY
# export CHAT_MODEL="gemini-2.0-flash"           # optional (this is the default)
# export EMBED_MODEL="gemini-embedding-001"      # optional (this is the default)

# ============================================================================
# Option D — Local models via Ollama (or any OpenAI-compatible server) — see SETUP.md
# ============================================================================
# export LLM_PROVIDER=openai
# export OPENAI_API_KEY="ollama"                 # any non-empty string
# export OPENAI_BASE_URL="http://localhost:11434/v1"
# export CHAT_MODEL="llama3.1"
# export EMBED_MODEL="nomic-embed-text"
# export ARD_RERANK=0                            # IMPORTANT for local: skip the slow 2nd-stage re-rank

# ============================================================================
# Optional, for any provider
# ============================================================================
# Set to a GCP project (with `gcloud auth application-default login`) to activate the
# BigQuery-backed population sources (sec-bq, irs-990-bq, census-acs-bq). Leave empty to skip them.
export GOOGLE_CLOUD_PROJECT=""
# api.data.gov key for the College Scorecard source (else it falls back to DEMO_KEY, which rate-limits).
export DATA_GOV_API_KEY=""
# US Census API key — REQUIRED for the census source (the API now rejects keyless requests).
# Free at https://api.census.gov/data/key_signup.html
export CENSUS_API_KEY=""

# --- ranking (the ARD Agent Finder's second-stage re-rank) -------------------------------------
# Ranking is the token-heavy discovery stage. The finder batches complementary query phrasings
# into one embedding request and one rerank, and the response contains only table indices/scores —
# no prose — so reasoning and output are deliberately bounded. On the 193-case routing corpus the
# defaults cut measured discovery cost from about $0.00051 to $0.00013 per question.
# export RERANK_MODEL="openai/gpt-oss-20b"     # default: falls back to CHAT_MODEL
# export SYNTHESIS_MODEL="openai/gpt-4o"       # optional: high-quality final prose model
# export ARD_RERANK_REASONING_EFFORT="low"     # default: low; finder needs only compact indices
# export ARD_RERANK_MAX_TOKENS=400              # hard cap for the finder JSON, not answer prose
#
# OpenRouter provider routing. "throughput" (the default) picks the fastest provider, which for a
# multi-provider model can cost ~2.4x more per token than the cheapest one; "price" picks the
# cheapest and is the better default for batch work. Set empty to let OpenRouter choose.
# export LLM_PROVIDER_SORT="throughput"        # throughput | price | latency | ""
