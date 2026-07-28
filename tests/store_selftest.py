#!/usr/bin/env python3
"""Regression test for the store layer — the tiered hot/cold path and the cloud adapters.

Real S3/GCS/Azure need credentials, so the TIERED round-trip is exercised through the
`localdir` object adapter, which has identical semantics (get/put/list/delete over a
namespace). This proves the promotion/eviction/rehydrate logic without a live bucket; the
cloud adapters are additionally checked to import and construct.

    ARD_STORE=localdir python3 tests/store_selftest.py
"""
import os, sys, json, gzip
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ARD_STORE"] = "localdir"

import store, store_backends

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  — ' + detail) if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


def main():
    info = store.backend_info()
    print(f"backend: {info['backend']} — {info['detail']}\n")
    b = store.backend()
    check("tiered backend selected", b.name == "tiered", f"got {b.name}")

    obs = [{"entity": store.eid("fips", "06001"), "entity_name": "Alameda", "value": 12.3},
           {"entity": store.eid("fips", "06003"), "entity_name": "Alpine", "value": 45.6}]
    k = store.key("selftest/m", "county", "2024")

    # 1. write lands in BOTH tiers
    store.put(k, obs, {"op": "selftest"})
    check("write -> hot tier (sqlite)", (b.local.read(k) or {}).get("n") == 2)
    check("write -> cold tier (object)", any(n.endswith(".json.gz") for n in b.remote.list()))

    # 2. evict hot tier only; read must REHYDRATE from cold
    b.local.delete("selftest/m")
    check("hot tier evicted", b.local.read(k) is None)
    got = store.get(k, max_age_days=None)
    check("read rehydrates from cold", got is not None and got["n"] == 2)
    check("hot tier repopulated", (b.local.read(k) or {}).get("n") == 2)

    # 3. compression on the cold tier
    obj = next((n for n in b.remote.list() if n.endswith(".json.gz")), None)
    if obj:
        raw = b.remote.get(obj)
        dec = gzip.decompress(raw)
        check("cold object is gzip and round-trips", json.loads(dec)["n"] == 2,
              f"{len(raw)}B gz vs {len(dec)}B raw")

    # 4. native SQL join across two materialized measures (hot tier)
    k2 = store.key("selftest/m2", "county", "2024")
    store.put(k2, [{"entity": store.eid("fips", "06001"), "entity_name": "Alameda", "value": 99.0}], {})
    joined = b.local.align_sql({"m": k, "m2": k2})
    check("align_sql intersects on entity", joined is not None and len(joined) == 1
          and joined[0].get("m") == 12.3 and joined[0].get("m2") == 99.0)

    # 5. cloud adapters are wired: they either construct, or fail ONLY for a missing SDK/credentials
    # (an environment limitation, not a code defect). A different error is a real failure.
    ENVIRONMENT = ("credential", "default credentials", "not installed", "no module named",
                   "connection string", "AZURE_STORAGE")
    for kind, sdk in [("s3", "boto3"), ("gcs", "google.cloud.storage"), ("azure", "azure.storage.blob")]:
        cls = {"s3": store_backends.S3Adapter, "gcs": store_backends.GCSAdapter,
               "azure": store_backends.AzureBlobAdapter}[kind]
        try:
            cls("ard-selftest-bucket", "p/")            # constructs a client; makes no request
            check(f"{kind} adapter constructs", True)
        except SystemExit as e:                          # our own "needs pip install X" message
            print(f"  skip {kind} adapter — SDK not installed")
        except Exception as e:
            env = any(w in str(e).lower() for w in ENVIRONMENT)
            if env:
                print(f"  skip {kind} adapter — needs credentials/SDK ({str(e)[:50].strip()}…)")
            else:
                check(f"{kind} adapter constructs", False, str(e)[:70])

    # cleanup
    b.local.delete("selftest/"); [b.remote.delete(n) for n in b.remote.list()]
    print("\n" + ("ALL PASS" if not FAILS else f"FAILURES: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
