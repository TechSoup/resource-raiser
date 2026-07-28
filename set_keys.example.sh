#!/usr/bin/env bash
# Copy this file to set_keys.sh (which is gitignored) and fill in ONE provider's values.
# run.sh sources set_keys.sh automatically; or export these some other way.
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
# ============================================================================
# export GEMINI_API_KEY="..."                    # or GOOGLE_API_KEY
# export CHAT_MODEL="gemini-2.0-flash"           # optional (this is the default)
# export EMBED_MODEL="text-embedding-004"        # optional (this is the default)

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
