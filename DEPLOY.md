# Deploying to a server

Two processes: the **ARD Agent Finder** (holds the embedding index, answers `POST /search`) and the
**harness** (the web UI and `POST /ask`). The finder is an internal dependency of the harness — only
the harness needs to be reachable from outside.

## Configuration

Everything is environment variables; nothing is read from a config file except `set_keys.sh`, which
is only a convenience for local use. On a server, set these in the platform's own settings.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_PROVIDER` | the model provider (OpenRouter here) |
| `CHAT_MODEL`, `EMBED_MODEL`, `RERANK_MODEL` | models; ranking is split out because it is the token-heavy stage |
| `GRANTS_URL` | Postgres holding the IRS 990 grant graph |
| `CENSUS_API_KEY` | Census ACS (free key) |
| `HARNESS_BIND_HOST` | `0.0.0.0` for the public ASGI app on a server |
| `AGENT_FINDER_BIND_HOST` | finder bind address; normally leave it on `127.0.0.1` |
| `PORT` / `WEBSITES_PORT` | listening port (App Service sets this) |
| `AGENT_FINDER_URL` | where the harness finds the finder (default `http://127.0.0.1:8088`) |
| `ARD_PREFILTER`, `ARD_RERANK` | discovery cost/quality dials — see `registry/index.py` |
| `ASK_LIMIT_PER_DAY` | questions per source per UTC day (default 200; `0` disables) |
| `TRUST_PROXY` | set to `1` **only** when a proxy you control sets `X-Forwarded-For` |

## Health check

`GET /healthz` -> `{"ok": true, "tables": 8925, "agent_finder": true}`

It verifies the index is loaded and the finder answers. It deliberately makes **no LLM call**: a
health probe that costs money on every poll is a bill, not a health check. Point the load balancer
or App Service health check here, not at `/`.

## The index has to exist before the harness is useful

A fresh clone has no `registry/vectors.npy` and no generated leaves — they are gitignored because
they are derived. Either:

1. **Build on the server** (`./run.sh` does it on first start): runs the generators, then embeds
   ~8,900 descriptors. Takes ~10 minutes and costs one embedding pass. Needs outbound network to
   the taxonomies (FASB, Census, Treasury, CDC) and to the embedding provider.
2. **Ship the built artifacts** — copy `sources/` and `registry/{vectors.npy,meta.json}` from a
   machine that has them. Faster, no build-time API access needed, and gives every instance a
   byte-identical index.

Option 2 is the better answer for more than one instance: the index is keyed on the embedding
model, so instances that build separately are consistent only if they use the same model.

Generated leaves and registry artifacts are gitignored, so **a `git pull` is not a complete
deployment when a generator or its inputs changed**. Build a coherent release with:

```bash
set -a; source ./set_keys.sh; set +a
python3 tools/build_registry_release.py
```

Then either run that command on the VM before restarting, or copy `sources/` and
`registry/current/` together from the build machine. `run.sh` verifies the release-builder stamp,
the descriptor corpus, and the active index before serving; it fails with the rebuild command
instead of silently using stale artifacts. Restart both `rr-finder` and `rr-harness` after
publishing because the finder keeps the index in memory.

## Database

The grant graph lives in Azure Database for PostgreSQL (`rr-grants-pg`, `tsnlw-rg`, westus2,
Standard_B1ms). Everything else is fetched live per query and needs no database.

The firewall has an `AllowAzureServices` rule (`0.0.0.0`), which is what lets an Azure VM or App
Service connect without allow-listing an IP. **Note what that rule actually means:** it permits
connections originating from any Azure IP, including other tenants — the password is then the only
thing standing in front of the database. For anything beyond a demo, use VNet integration with a
private endpoint and delete both this rule and the laptop IP rule.

The population-scale answers read from precomputed `agg_*` rollups rather than scanning the edge
table; on the B1ms SKU the by-cause join takes ~280s live and ~6s from the rollup. Rebuild them
with `python3 tools/grants_to_postgres.py --rollups-only` if the edge table is ever reloaded.

## Process model

The public service is the single event-loop-native ASGI application:

```bash
python3 -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8099}" --workers 1
```

One worker handles concurrent users with async tasks and shared async provider clients. Scale out
with App Service instances; do not add Python query threads or multiple workers that independently
duplicate the in-memory index, quota state, and provider pools. Keep the Agent Finder on loopback.

## Not production-hardened

Worth being explicit, since it is easy to mistake this for more than it is:

- Uvicorn serves plain HTTP. Terminate TLS and perform graceful instance rotation at the platform
  or reverse proxy.
- `POST /ask` is **unauthenticated**. A per-source daily cap (`ASK_LIMIT_PER_DAY`, default 200)
  limits the damage one runaway script can do, and returns `429` with `Retry-After` once hit. It
  counts failed and refused questions too, because those cost money as well.

  Set `TRUST_PROXY=1` **only** behind a proxy you control. `X-Forwarded-For` is client-supplied, so
  trusting it unconditionally makes the cap pointless — a caller can send a new value per request
  and get a new quota each time. With it off, everyone behind a proxy shares the proxy's quota;
  with it on, the *last* XFF entry is used, which is the one your proxy appended.

  Counts live in process memory, so a restart clears them and multiple instances count separately.
  That is enough to stop a loop, not a determined attacker.
- Secrets are read from the environment. `set_keys.sh` holds them in plaintext for local work; on
  Azure use App Settings or Key Vault with a managed identity.
