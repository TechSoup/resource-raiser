#!/usr/bin/env bash
# Bring up the ARD data-query stack (Agent Finder + Harness/Web UI).
#
#   ./run.sh
#
# FIRST RUN does a one-time build (~10 min): it regenerates the machine-generated table
# descriptions from their public taxonomies/APIs, then embeds the ARD index. Later runs skip
# straight to serving. Requires Azure OpenAI credentials — see set_keys.example.sh.
set -e
cd "$(dirname "$0")"

# --- credentials --------------------------------------------------------------------
# Put your keys in ./set_keys.sh (gitignored; copy set_keys.example.sh). If you export the
# vars some other way, that's fine too — this just sources the file when it exists.
if [ -f set_keys.sh ]; then set -a; source set_keys.sh; set +a; fi
if [ -z "${AZURE_OPENAI_API_KEY:-}${OPENAI_API_KEY:-}${GEMINI_API_KEY:-}${GOOGLE_API_KEY:-}" ]; then
  echo "ERROR: No LLM credentials set. Configure Azure OpenAI, OpenAI, or Gemini in set_keys.sh" >&2
  echo "       (copy set_keys.example.sh to set_keys.sh and fill in one provider)." >&2
  exit 1
fi
# GOOGLE_CLOUD_PROJECT is OPTIONAL — set it to activate the BigQuery-backed population sources.
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-}"

# --- one-time build: generate tables + embed the ARD index --------------------------
if [ ! -f registry/vectors.npy ]; then
  echo "First run — building table descriptions and ARD index (~10 min)…"
  python3 tools/gen_sec_okf.py        # SEC EDGAR us-gaap concepts (from the FASB taxonomy)
  python3 tools/gen_census_okf.py     # Census ACS Data Profile variables
  python3 tools/gen_treasury_okf.py   # Treasury FiscalData series
  python3 tools/gen_cdc_okf.py        # CDC PLACES measures
  python3 tools/gen_np_okf.py         # IRS 990 nonprofit fields
  python3 registry/index.py build     # embed every leaf -> registry/vectors.npy + meta.json
  echo "Build complete."
fi

# --- serve --------------------------------------------------------------------------
pkill -f "agent_finder.py" 2>/dev/null || true
pkill -f "harness.py --serve" 2>/dev/null || true
sleep 1
nohup python3 agent_finder.py    > /tmp/ard_agent_finder.log 2>&1 &
nohup python3 harness.py --serve > /tmp/ard_harness.log      2>&1 &

up=""
for i in $(seq 1 30); do
  if curl -s -m2 http://127.0.0.1:8088/ >/dev/null 2>&1 && curl -s -m2 http://127.0.0.1:8099/ >/dev/null 2>&1; then
    up=1; break
  fi
  sleep 1
done
if [ -z "$up" ]; then
  echo "ERROR: a service did not come up. Most often this is missing LLM credentials." >&2
  echo "--- last lines of /tmp/ard_harness.log ---" >&2; tail -n 15 /tmp/ard_harness.log >&2
  echo "--- last lines of /tmp/ard_agent_finder.log ---" >&2; tail -n 8 /tmp/ard_agent_finder.log >&2
  exit 1
fi

echo "Agent Finder  : http://127.0.0.1:8088/  (POST /search)"
echo "Harness/Web UI: http://127.0.0.1:8099/  (web UI + POST /ask)"
echo "Logs: /tmp/ard_agent_finder.log  /tmp/ard_harness.log"
echo "Stop: pkill -f agent_finder.py; pkill -f 'harness.py --serve'"
echo
echo "Note: the IRS 990 grant-graph queries need a one-time data extraction:"
echo "      python3 tools/grants_download.py   (downloads ~12GB, builds data/990/grants.sqlite)"
