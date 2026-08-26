# Native Azure App Service deployment

This deployment uses Azure App Service's managed Python 3.13 runtime. There is no Docker image,
container registry, or Redis dependency. Each Web App instance runs one Uvicorn event loop for
Neural KG and one loopback-only Agent Finder; App Service scales those complete instances.

## Build the code-release ZIP

The generated descriptors and `registry/current/` are part of the release even though they are
gitignored. Build them before packaging, then create a ZIP whose root is the application root:

```bash
python tools/build_registry_release.py
python deploy/package_webapp.py dist/resource-raiser.zip
```

The packager runs `registry/index.py verify --release`, collects the tracked/untracked application
files plus every generated source descriptor, dereferences exactly the active registry generation
into `registry/current/`, and writes a SHA-256 checksum beside the ZIP. It excludes `.venv`, local
caches, data, and ignored secret files. Production startup verifies the same release again and never
generates or repairs artifacts.

Do not deploy a ZIP made by archiving Git alone: the generated descriptors and registry are
intentionally not in Git, and Finder cannot start without them.

## Provision and deploy

Create the Linux Web App and Application Insights resource:

```bash
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/main.bicep \
  --parameters appName="$APP_NAME" secretSettings="@deploy/secrets.parameters.json"
```

Pass provider credentials as Key Vault reference strings in the secure `secretSettings` object.
Fixed deployment settings such as telemetry, startup, and the scale ceiling cannot be overridden
there. Then deploy the release ZIP:

```bash
az webapp deploy \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --src-path dist/resource-raiser.zip
```

The Bicep template enables `SCM_DO_BUILD_DURING_DEPLOYMENT`, so App Service's Oryx build installs
`requirements.txt` into a Linux virtual environment during ZIP deployment. Do not package the local
`.venv`; Python virtual environments are not portable between macOS and App Service Linux. Run from
package is also intentionally not enabled because App Service does not support that mode for Python
applications that need build automation.

The configured startup command is `bash deploy/start-webapp.sh`. It verifies the registry, starts
Finder only on loopback, waits for Finder readiness, and starts `python -m uvicorn app:app` on port
8000 with one worker. App Service probes `/healthz`; ARR affinity is disabled. During rotation the
script forwards termination to both servers and bounds Uvicorn's graceful drain at 30 seconds.

## Scaling invariants and limits

`maxInstances` is written to `WEBAPP_MAX_INSTANCES` and is also the autoscale ceiling. Do not change
one without the other. Every instance paces SEC at:

```
SEC_FLEET_REQUESTS_PER_SECOND / WEBAPP_MAX_INSTANCES
```

This is conservative: one instance uses only its fixed fleet share, while a fully scaled fleet
cannot exceed the configured total through normal pacing. SEC `Retry-After` responses still take
precedence. Separate deployments using the same SEC identity must divide the fleet budget between
themselves.

`ASK_LIMIT_PER_DAY` remains an in-memory, per-instance damage limiter. It is not a global quota and
must not be presented as authentication or exact billing enforcement. If exact tenant limits become
necessary, enforce authenticated quotas at API Management or another gateway. Finder's own daily
cap is disabled because it is loopback-only and every public request already passes through Web App
admission.

Application Insights is configured from the provisioned connection string. HTTP spans, runtime
metrics, and bounded request-event logs go through Azure Monitor's OpenTelemetry distribution.
`/healthz` exposes instance id, loop lag, active requests, provider permits, telemetry drops, and
the effective SEC pacing values.

Autoscale uses request volume rather than CPU because the workload is I/O-bound. Its bootstrap
thresholds are explicit Bicep parameters, not measured production limits. Before changing them,
use `tools/calibrate_webapp.py` with explicit request, concurrency, and estimated-cost ceilings.
Stop on provider 429s or growing loop lag; do not infer production limits from no-network unit tests.
