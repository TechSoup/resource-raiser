#!/usr/bin/env python3
"""Storage backends for the materialized commons.

The commons is a relational thing — `(entity, measure, period) -> value` is a table, not a
document — so the backend is swappable and chosen by DEPLOYMENT rather than hardcoded:

    local / macOS         sqlite   single file, stdlib, indexed, real SQL joins
    dev / inspection      json     zero-dependency files, human-readable, easy to delete
    GCP (Cloud Run/GAE)   bigquery managed, scales past one machine's disk
    Azure (App Service)   postgres managed Postgres via DATABASE_URL

Selection order: explicit `ARD_STORE` env var, then cloud env signals, then sqlite.
Every backend implements the same five methods, so `store.py` and its callers never change.
"""
import os, json, time, platform


class Backend:
    name = "base"

    def read(self, key):
        """-> {'n','fetched_at','meta','observations'} or None"""
        raise NotImplementedError

    def write(self, key, observations, meta):
        raise NotImplementedError

    def list(self):
        """-> [{'key','rows','age_hours'}]"""
        raise NotImplementedError

    def delete(self, prefix=""):
        raise NotImplementedError

    def describe(self):
        return self.name


# --- json: zero dependency, human-inspectable ------------------------------------
class JsonBackend(Backend):
    name = "json"

    def __init__(self, root):
        import re, hashlib
        self._re, self._hash = re, hashlib
        self.dir = os.path.join(root, "cache")

    def _path(self, key):
        safe = self._re.sub(r"[^A-Za-z0-9._-]", "_", key)[:70]
        return os.path.join(self.dir, f"{safe}.{self._hash.md5(key.encode()).hexdigest()[:10]}.json")

    def read(self, key):
        p = self._path(key)
        if not os.path.exists(p):
            return None
        try:
            return json.load(open(p))
        except Exception:
            return None

    def write(self, key, observations, meta):
        os.makedirs(self.dir, exist_ok=True)
        e = {"key": key, "fetched_at": time.time(), "n": len(observations),
             "meta": meta or {}, "observations": observations}
        tmp = self._path(key) + ".tmp"
        json.dump(e, open(tmp, "w"))
        os.replace(tmp, self._path(key))
        return e

    def list(self):
        if not os.path.isdir(self.dir):
            return []
        out = []
        for f in sorted(os.listdir(self.dir)):
            if not f.endswith(".json"):
                continue
            try:
                e = json.load(open(os.path.join(self.dir, f)))
            except Exception:
                continue
            out.append({"key": e.get("key"), "rows": e.get("n"),
                        "age_hours": round((time.time() - e.get("fetched_at", 0)) / 3600, 1)})
        return out

    def delete(self, prefix=""):
        n = 0
        for e in self.list():
            if (e["key"] or "").startswith(prefix):
                p = self._path(e["key"])
                if os.path.exists(p):
                    os.remove(p)
                    n += 1
        return n

    def describe(self):
        return f"json files in {self.dir}"


# --- sqlite: the local default ---------------------------------------------------
class SqliteBackend(Backend):
    """One indexed file. Observations are rows, so cross-measure joins and aggregates are SQL
    rather than full scans in Python, and a partition can be upserted without rewriting the table."""
    name = "sqlite"

    DDL = """
    CREATE TABLE IF NOT EXISTS observations (
      measure TEXT NOT NULL, grain TEXT NOT NULL, vintage TEXT NOT NULL,
      entity TEXT NOT NULL, entity_name TEXT, value REAL, unit TEXT, source TEXT,
      PRIMARY KEY (measure, grain, vintage, entity)
    );
    CREATE INDEX IF NOT EXISTS idx_obs_measure ON observations(measure, grain, vintage);
    CREATE INDEX IF NOT EXISTS idx_obs_entity  ON observations(entity);
    CREATE TABLE IF NOT EXISTS materialized (
      key TEXT PRIMARY KEY, measure TEXT, grain TEXT, vintage TEXT,
      fetched_at REAL, n INTEGER, meta TEXT
    );
    """

    def __init__(self, path):
        import sqlite3
        self.sqlite3 = sqlite3
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._conn() as c:
            c.executescript(self.DDL)

    def _conn(self):
        c = self.sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")        # concurrent readers alongside a writer
        return c

    @staticmethod
    def _split(key):
        parts = (key.split("|") + ["", "", ""])[:3]
        return parts[0], parts[1], parts[2]

    def read(self, key):
        m, g, v = self._split(key)
        with self._conn() as c:
            row = c.execute("SELECT fetched_at, n, meta FROM materialized WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            obs = [{"entity": e, "entity_name": en, "value": val, "unit": u, "source": s}
                   for e, en, val, u, s in c.execute(
                       "SELECT entity, entity_name, value, unit, source FROM observations "
                       "WHERE measure=? AND grain=? AND vintage=?", (m, g, v))]
        return {"key": key, "fetched_at": row[0], "n": row[1],
                "meta": json.loads(row[2] or "{}"), "observations": obs}

    def write(self, key, observations, meta):
        m, g, v = self._split(key)
        now = time.time()
        with self._conn() as c:
            c.execute("DELETE FROM observations WHERE measure=? AND grain=? AND vintage=?", (m, g, v))
            c.executemany(
                "INSERT OR REPLACE INTO observations "
                "(measure,grain,vintage,entity,entity_name,value,unit,source) VALUES (?,?,?,?,?,?,?,?)",
                [(m, g, v, o.get("entity"), o.get("entity_name"), o.get("value"),
                  o.get("unit"), o.get("source")) for o in observations if o.get("entity")])
            c.execute("INSERT OR REPLACE INTO materialized (key,measure,grain,vintage,fetched_at,n,meta) "
                      "VALUES (?,?,?,?,?,?,?)", (key, m, g, v, now, len(observations),
                                                 json.dumps(meta or {})))
        return {"key": key, "fetched_at": now, "n": len(observations),
                "meta": meta or {}, "observations": observations}

    def list(self):
        with self._conn() as c:
            return [{"key": k, "rows": n, "age_hours": round((time.time() - f) / 3600, 1)}
                    for k, f, n in c.execute("SELECT key, fetched_at, n FROM materialized ORDER BY key")]

    def delete(self, prefix=""):
        n = 0
        with self._conn() as c:
            for (k,) in c.execute("SELECT key FROM materialized").fetchall():
                if (k or "").startswith(prefix):
                    m, g, v = self._split(k)
                    c.execute("DELETE FROM observations WHERE measure=? AND grain=? AND vintage=?", (m, g, v))
                    c.execute("DELETE FROM materialized WHERE key=?", (k,))
                    n += 1
        return n

    def align_sql(self, labelled):
        """Native SQL join across measures — the payoff of a relational backend: the intersection
        happens in the store, not by loading every measure into Python. `labelled` = {label: key}."""
        items = list(labelled.items())
        if len(items) < 2:
            return None
        sel = ["o0.entity", "o0.entity_name"]
        frm, where, params = [], [], []
        for i, (label, key) in enumerate(items):
            m, g, v = self._split(key)
            a = f"o{i}"
            frm.append(f"observations {a}" if i == 0 else f"JOIN observations {a} ON {a}.entity = o0.entity")
            where += [f"{a}.measure=?", f"{a}.grain=?", f"{a}.vintage=?", f"{a}.value IS NOT NULL"]
            params += [m, g, v]
            if i:
                sel.append(f"{a}.value")
            else:
                sel.insert(2, f"{a}.value")
        q = f"SELECT {', '.join(sel)} FROM {' '.join(frm)} WHERE {' AND '.join(where)} ORDER BY o0.entity"
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        labels = [l for l, _ in items]
        return [{"entity": r[0], "entity_name": r[1],
                 **{labels[i]: r[2 + i] for i in range(len(labels))}} for r in rows]

    def describe(self):
        return f"sqlite at {self.path}"


# --- managed cloud backends -----------------------------------------------------
class BigQueryBackend(Backend):
    """GCP. Chosen when running on Cloud Run / App Engine / GKE with a project set."""
    name = "bigquery"

    def __init__(self, dataset, project=None):
        try:
            from google.cloud import bigquery            # noqa: F401
        except ImportError as e:
            raise SystemExit(
                "ARD_STORE=bigquery needs google-cloud-bigquery installed, and a dataset in "
                "ARD_STORE_DATASET. Table schema mirrors the sqlite backend: "
                "observations(measure,grain,vintage,entity,entity_name,value,unit,source). "
                f"({e})")
        self.dataset, self.project = dataset, project

    def _todo(self, *a, **k):
        raise SystemExit(
            f"the {self.name} backend is declared but not implemented yet. Its schema mirrors "
            "sqlite: observations(measure,grain,vintage,entity,entity_name,value,unit,source) "
            "plus materialized(key,fetched_at,n,meta). Implement read/write/list/delete in "
            "store_backends.py, or run with ARD_STORE=sqlite.")

    read = write = list = delete = _todo

    def describe(self):
        return f"bigquery {self.project}.{self.dataset}"


class PostgresBackend(Backend):
    """Azure / any managed Postgres, selected from DATABASE_URL."""
    name = "postgres"

    def __init__(self, dsn):
        try:
            import psycopg                              # noqa: F401
        except ImportError as e:
            raise SystemExit(
                "ARD_STORE=postgres needs psycopg installed and DATABASE_URL set. Same schema as "
                f"the sqlite backend. ({e})")
        self.dsn = dsn

    def _todo(self, *a, **k):
        raise SystemExit(
            f"the {self.name} backend is declared but not implemented yet. Its schema mirrors "
            "sqlite: observations(measure,grain,vintage,entity,entity_name,value,unit,source) "
            "plus materialized(key,fetched_at,n,meta). Implement read/write/list/delete in "
            "store_backends.py, or run with ARD_STORE=sqlite.")

    read = write = list = delete = _todo

    def describe(self):
        return "postgres via DATABASE_URL"



# --- object-storage tier: the cheap option on every cloud -------------------------
# Materialized tables are IMMUTABLE per (measure, grain, vintage), which is exactly what
# object storage is good at. For an intermittently-queried service, IDLE cost dominates,
# and that is where managed databases lose:
#
#   store                         idle/month     per GB-month   notes
#   S3 / GCS / Azure Blob         $0.00          ~$0.02         pay only for what you keep
#   BigQuery                      $0.00          $0.02          1 TB/month of queries free
#   DynamoDB on-demand            $0.00          $0.25/M reads  no joins
#   Cloud SQL / RDS / Azure PG    $12-50         extra          billed while idle
#   Aurora Serverless v2 (0.5ACU) ~$43           extra          billed while idle
#
# This commons is ~2.6 MB today and would be a few hundred MB fully materialized, so the
# object tier costs cents per month on all three clouds while a provisioned database costs
# tens of dollars. Reach for managed SQL only when several writers need transactions.

class ObjectAdapter:
    """Minimal object-store interface. Each cloud adapter is a thin wrapper over this."""
    name = "object"

    def get(self, key):
        raise NotImplementedError

    def put(self, key, data):
        raise NotImplementedError

    def list(self, prefix=""):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError


class LocalDirAdapter(ObjectAdapter):
    """Filesystem stand-in with object-store semantics — lets the tiered path be exercised
    and tested without cloud credentials."""
    name = "localdir"

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _p(self, key):
        return os.path.join(self.root, key.replace("/", "__"))

    def get(self, key):
        p = self._p(key)
        return open(p, "rb").read() if os.path.exists(p) else None

    def put(self, key, data):
        tmp = self._p(key) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self._p(key))

    def list(self, prefix=""):
        return [f.replace("__", "/") for f in sorted(os.listdir(self.root))
                if f.replace("__", "/").startswith(prefix) and not f.endswith(".tmp")]

    def delete(self, key):
        p = self._p(key)
        if os.path.exists(p):
            os.remove(p)

    def describe(self):
        return f"local dir {self.root} (object-store semantics)"


class S3Adapter(ObjectAdapter):
    """AWS. Cheapest durable option: S3 Standard ~$0.023/GB-month, no idle cost.
    Credentials come from the normal chain (env, profile, or instance role)."""
    name = "s3"

    def __init__(self, bucket, prefix=""):
        import boto3
        self.c = boto3.client("s3")
        self.bucket, self.prefix = bucket, prefix

    def _k(self, key):
        return f"{self.prefix}{key}"

    def get(self, key):
        try:
            return self.c.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()
        except self.c.exceptions.NoSuchKey:
            return None
        except Exception:
            return None

    def put(self, key, data):
        self.c.put_object(Bucket=self.bucket, Key=self._k(key), Body=data,
                          ContentType="application/json", ContentEncoding="gzip")

    def list(self, prefix=""):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._k(prefix)}
            if token:
                kw["ContinuationToken"] = token
            r = self.c.list_objects_v2(**kw)
            out += [o["Key"][len(self.prefix):] for o in r.get("Contents", [])]
            token = r.get("NextContinuationToken")
            if not r.get("IsTruncated"):
                return out

    def delete(self, key):
        self.c.delete_object(Bucket=self.bucket, Key=self._k(key))

    def describe(self):
        return f"s3://{self.bucket}/{self.prefix}"


class GCSAdapter(ObjectAdapter):
    """GCP. Cloud Storage Standard ~$0.020/GB-month, no idle cost. Uses ADC."""
    name = "gcs"

    def __init__(self, bucket, prefix=""):
        from google.cloud import storage
        self.b = storage.Client().bucket(bucket)
        self.bucket, self.prefix = bucket, prefix

    def get(self, key):
        blob = self.b.blob(self.prefix + key)
        return blob.download_as_bytes() if blob.exists() else None

    def put(self, key, data):
        self.b.blob(self.prefix + key).upload_from_string(data, content_type="application/json")

    def list(self, prefix=""):
        return [b.name[len(self.prefix):] for b in self.b.list_blobs(prefix=self.prefix + prefix)]

    def delete(self, key):
        blob = self.b.blob(self.prefix + key)
        if blob.exists():
            blob.delete()

    def describe(self):
        return f"gs://{self.bucket}/{self.prefix}"


class AzureBlobAdapter(ObjectAdapter):
    """Azure. Blob Storage Hot ~$0.018/GB-month, no idle cost.
    Connection string in AZURE_STORAGE_CONNECTION_STRING, or account URL + DefaultAzureCredential."""
    name = "azureblob"

    def __init__(self, container, prefix=""):
        from azure.storage.blob import BlobServiceClient
        conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        if conn:
            svc = BlobServiceClient.from_connection_string(conn)
        else:
            from azure.identity import DefaultAzureCredential
            acct = os.getenv("AZURE_STORAGE_ACCOUNT")
            svc = BlobServiceClient(f"https://{acct}.blob.core.windows.net",
                                    credential=DefaultAzureCredential())
        self.cc = svc.get_container_client(container)
        try:
            self.cc.create_container()
        except Exception:
            pass
        self.container, self.prefix = container, prefix

    def get(self, key):
        try:
            return self.cc.download_blob(self.prefix + key).readall()
        except Exception:
            return None

    def put(self, key, data):
        self.cc.upload_blob(self.prefix + key, data, overwrite=True)

    def list(self, prefix=""):
        return [b.name[len(self.prefix):] for b in self.cc.list_blobs(name_starts_with=self.prefix + prefix)]

    def delete(self, key):
        try:
            self.cc.delete_blob(self.prefix + key)
        except Exception:
            pass

    def describe(self):
        return f"azure://{self.container}/{self.prefix}"


class TieredBackend(Backend):
    """SQLite hot tier in front of an object-storage cold tier.

    read : local sqlite -> object store (hydrating local on the way back) -> miss
    write: local sqlite AND object store

    The hot tier keeps lookups and cross-measure SQL joins fast; the cold tier makes the
    commons durable, shareable between instances, and nearly free to keep. A cold-tier hit
    still avoids re-querying the upstream API, which is the expensive thing."""
    name = "tiered"

    def __init__(self, sqlite_path, adapter):
        self.local = SqliteBackend(sqlite_path)
        self.remote = adapter

    @staticmethod
    def _obj(key):
        import re as _re
        return _re.sub(r"[^A-Za-z0-9._-]", "_", key)[:120] + ".json.gz"

    def read(self, key):
        hit = self.local.read(key)
        if hit is not None:
            return hit
        raw = self.remote.get(self._obj(key))
        if raw is None:
            return None
        import gzip
        e = json.loads(gzip.decompress(raw).decode())
        self.local.write(key, e.get("observations", []), e.get("meta"))   # hydrate the hot tier
        return e

    def write(self, key, observations, meta):
        e = self.local.write(key, observations, meta)
        import gzip
        self.remote.put(self._obj(key), gzip.compress(json.dumps(e).encode(), 6))
        return e

    def list(self):
        seen = {r["key"]: r for r in self.local.list()}
        for name in self.remote.list():
            if name.endswith(".json.gz") and name not in seen:
                seen.setdefault(name, {"key": name, "rows": None, "age_hours": None})
        return list(seen.values())

    def delete(self, prefix=""):
        n = self.local.delete(prefix)
        for name in self.remote.list():
            self.remote.delete(name)
        return n

    def align_sql(self, labelled):
        return self.local.align_sql(labelled)

    def describe(self):
        return f"sqlite hot tier + {self.remote.describe()}"


# --- selection -------------------------------------------------------------------
def detect():
    """Which backend this deployment should use, and why.

    Order: explicit ARD_STORE, then cloud signals, then sqlite. On every cloud the default
    is the object-storage tier, because it has NO idle cost — the dominant expense for an
    intermittently-queried service."""
    explicit = os.getenv("ARD_STORE")
    if explicit:
        return explicit, f"ARD_STORE={explicit}"
    bucket = os.getenv("ARD_BUCKET")
    if os.getenv("K_SERVICE") or os.getenv("GAE_ENV") or os.getenv("GOOGLE_CLOUD_PROJECT"):
        return ("gcs" if bucket else "sqlite"), ("GCP detected" if bucket else
                                                "GCP detected but ARD_BUCKET unset — staying local")
    if os.getenv("WEBSITE_INSTANCE_ID") or os.getenv("AZURE_STORAGE_ACCOUNT") or \
            os.getenv("AZURE_STORAGE_CONNECTION_STRING"):
        return ("azure" if bucket else "sqlite"), ("Azure detected" if bucket else
                                                  "Azure detected but ARD_BUCKET unset — staying local")
    if os.getenv("AWS_EXECUTION_ENV") or os.getenv("ECS_CONTAINER_METADATA_URI") or \
            os.getenv("AWS_LAMBDA_FUNCTION_NAME") or (bucket and os.getenv("AWS_REGION")):
        return ("s3" if bucket else "sqlite"), ("AWS detected" if bucket else
                                                "AWS detected but ARD_BUCKET unset — staying local")
    if os.getenv("DATABASE_URL"):
        return "postgres", "DATABASE_URL is set"
    return "sqlite", f"local {platform.system()} — sqlite is stdlib and needs no service"


def build(root):
    kind, why = detect()
    sqlite_path = os.path.join(root, "cache", "commons.db")
    bucket = os.getenv("ARD_BUCKET", "")
    prefix = os.getenv("ARD_PREFIX", "ard-commons/")

    if kind == "json":
        return JsonBackend(root), why
    if kind == "sqlite":
        return SqliteBackend(sqlite_path), why
    if kind in ("s3", "gcs", "azure", "localdir"):
        if kind != "localdir" and not bucket:
            raise SystemExit(f"ARD_STORE={kind} needs ARD_BUCKET (the bucket/container name). "
                             "Optional: ARD_PREFIX (default 'ard-commons/').")
        try:
            adapter = {"s3": lambda: S3Adapter(bucket, prefix),
                       "gcs": lambda: GCSAdapter(bucket, prefix),
                       "azure": lambda: AzureBlobAdapter(bucket, prefix),
                       "localdir": lambda: LocalDirAdapter(os.path.join(root, "cache", "objstore"))}[kind]()
        except ImportError as e:
            sdk = {"s3": "boto3", "gcs": "google-cloud-storage", "azure": "azure-storage-blob"}[kind]
            raise SystemExit(f"ARD_STORE={kind} needs `pip install {sdk}` ({e})")
        return TieredBackend(sqlite_path, adapter), why
    if kind == "bigquery":
        return BigQueryBackend(os.getenv("ARD_STORE_DATASET", "ard_commons"),
                               os.getenv("GOOGLE_CLOUD_PROJECT")), why
    if kind == "postgres":
        return PostgresBackend(os.getenv("DATABASE_URL", "")), why
    raise SystemExit(f"unknown ARD_STORE={kind!r}; "
                     "use json|sqlite|s3|gcs|azure|localdir|bigquery|postgres")
