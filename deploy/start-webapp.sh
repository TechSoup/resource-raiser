#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python}"
export AGENT_FINDER_BIND_HOST="127.0.0.1"
export AGENT_FINDER_PORT="${AGENT_FINDER_PORT:-8088}"
export AGENT_FINDER_URL="http://127.0.0.1:${AGENT_FINDER_PORT}"
export HARNESS_BIND_HOST="0.0.0.0"
export SEARCH_LIMIT_PER_DAY="${SEARCH_LIMIT_PER_DAY:-0}"
WEB_PORT="${PORT:-${WEBSITES_PORT:-8000}}"

# Production boot never generates or repairs data. An incoherent code release fails readiness.
"$PYTHON" registry/index.py verify --release

finder_pid=""
web_pid=""
shutdown_children() {
  trap - TERM INT
  if [[ -n "$web_pid" ]] && kill -0 "$web_pid" 2>/dev/null; then kill -TERM "$web_pid"; fi
  if [[ -n "$finder_pid" ]] && kill -0 "$finder_pid" 2>/dev/null; then kill -TERM "$finder_pid"; fi
  if [[ -n "$web_pid" ]]; then wait "$web_pid" 2>/dev/null || true; fi
  if [[ -n "$finder_pid" ]]; then wait "$finder_pid" 2>/dev/null || true; fi
}
trap 'shutdown_children; exit 143' TERM INT

"$PYTHON" -m uvicorn agent_finder:app --host 127.0.0.1 \
  --port "$AGENT_FINDER_PORT" --workers 1 &
finder_pid=$!

ready=""
for _attempt in $(seq 1 60); do
  if ! kill -0 "$finder_pid" 2>/dev/null; then
    wait "$finder_pid"
    exit $?
  fi
  if "$PYTHON" -c "import httpx; httpx.get('${AGENT_FINDER_URL}/healthz', timeout=1).raise_for_status()" \
      >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ -z "$ready" ]]; then
  echo "Agent Finder did not become ready within 60 seconds" >&2
  shutdown_children
  exit 1
fi

"$PYTHON" -m uvicorn app:app --host "$HARNESS_BIND_HOST" --port "$WEB_PORT" \
  --workers 1 --timeout-graceful-shutdown "${GRACEFUL_SHUTDOWN_SECONDS:-30}" \
  --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}" &
web_pid=$!

set +e
wait "$web_pid"
status=$?
set -e
shutdown_children
exit "$status"
