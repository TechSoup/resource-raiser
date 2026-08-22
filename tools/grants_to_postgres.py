#!/usr/bin/env python3
"""Load the IRS 990 grant-edge table into managed Postgres.

The grant graph is the one source that cannot be fetched live — the IRS publishes no query API
for grants, only bulk e-file ZIPs (see tools/grants_download.py). Once built, the edge table is a
static 7.8M-row fact table, which is exactly the thing to hand to a managed database rather than
carry around as a 1.9 GB file.

    export GRANTS_URL="postgresql://user:pass@host/grants?sslmode=require"
    python3 tools/grants_to_postgres.py                    # create, load, index, verify
    python3 tools/grants_to_postgres.py --verify-only      # compare against the local sqlite

Loading uses COPY from a binary stream, not row-by-row INSERT: 7.8M inserts over a network round
trip each would take hours, while COPY streams the whole table in one.

Indexes are created AFTER the load. Building them during it would have Postgres maintaining four
B-trees on every row inserted, which costs more than building them once at the end.
"""
import os, sys, time, sqlite3, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQLITE = os.getenv("GRANTS_DB") or os.path.join(ROOT, "data", "990", "grants.sqlite")
NTEE_SQLITE = os.path.join(ROOT, "data", "990", "ntee.sqlite")
URL = os.getenv("GRANTS_URL") or os.getenv("DATABASE_URL")

COLS = ("funder_ein", "funder_name", "funder_state", "recipient_ein", "recipient_name",
        "recipient_state", "amount", "purpose", "tax_year", "form")

DDL = """
CREATE TABLE IF NOT EXISTS grant_edges (
  funder_ein     text,
  funder_name    text,
  funder_state   text,
  recipient_ein  text,
  recipient_name text,
  recipient_state text,
  amount         double precision,
  purpose        text,
  tax_year       integer,
  form           text
);
"""

# Mirrors the sqlite indexes. The two name indexes serve `LIKE '%NAME%'` lookups, which a plain
# B-tree cannot: an unanchored substring match needs a trigram index. Without pg_trgm those
# queries seq-scan 7.8M rows — on a 1-vCore server that is a ~2 minute query, i.e. a timeout.
#
# Index the COLUMN, not upper(column). grants.py emits `ILIKE`, and an expression index on
# upper(name) cannot serve `name ILIKE ...` — the planner needs the indexed expression to match
# the predicate. Indexing upper(name) here silently produced a seq scan despite the index existing.
INDEXES = [
    ("idx_ge_funder", "CREATE INDEX IF NOT EXISTS idx_ge_funder ON grant_edges(funder_ein)"),
    ("idx_ge_recip_ein", "CREATE INDEX IF NOT EXISTS idx_ge_recip_ein ON grant_edges(recipient_ein)"),
    ("idx_ge_funder_nm", "CREATE INDEX IF NOT EXISTS idx_ge_funder_nm_trgm ON grant_edges "
                         "USING gin (funder_name gin_trgm_ops)"),
    ("idx_ge_recip_nm", "CREATE INDEX IF NOT EXISTS idx_ge_recip_nm_trgm ON grant_edges "
                        "USING gin (recipient_name gin_trgm_ops)"),
    ("idx_ge_states", "CREATE INDEX IF NOT EXISTS idx_ge_states ON grant_edges(funder_state, recipient_state)"),
]

CHECKS = [
    ("row count", "SELECT COUNT(*) FROM grant_edges"),
    ("total amount", "SELECT ROUND(SUM(amount)) FROM grant_edges"),
    ("distinct funders", "SELECT COUNT(DISTINCT funder_ein) FROM grant_edges"),
    ("rows with recipient EIN", "SELECT COUNT(*) FROM grant_edges WHERE recipient_ein <> ''"),
]


def connect():
    import psycopg
    if not URL:
        raise SystemExit("set GRANTS_URL (or DATABASE_URL) to the Postgres connection string")
    return psycopg.connect(URL, autocommit=True)


def load(pg):
    src = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    total = src.execute("SELECT COUNT(*) FROM grant_edges").fetchone()[0]
    print(f"loading {total:,} edges from {SQLITE}")
    pg.execute("DROP TABLE IF EXISTS grant_edges")
    pg.execute(DDL)

    t0, n = time.time(), 0
    cur = src.execute(f"SELECT {','.join(COLS)} FROM grant_edges")
    with pg.cursor().copy(f"COPY grant_edges ({','.join(COLS)}) FROM STDIN") as cp:
        while True:
            rows = cur.fetchmany(50_000)
            if not rows:
                break
            for r in rows:
                cp.write_row(r)
            n += len(rows)
            el = time.time() - t0
            print(f"  {n:,}/{total:,}  ({n / total:.0%})  {n / max(el, 1e-9):,.0f} rows/s", flush=True)
    print(f"copied {n:,} rows in {time.time() - t0:.0f}s")
    return n


def load_ntee(pg):
    """The BMF NTEE lookup (ein -> cause), for the by-cause queries. On sqlite it is a second file
    the query ATTACHes; Postgres has no ATTACH, so it becomes a table in the same database."""
    if not os.path.exists(NTEE_SQLITE):
        print("no ntee.sqlite — skipping the cause lookup")
        return 0
    src = sqlite3.connect(f"file:{NTEE_SQLITE}?mode=ro", uri=True)
    total = src.execute("SELECT COUNT(*) FROM ntee").fetchone()[0]
    print(f"loading {total:,} NTEE rows")
    pg.execute("DROP TABLE IF EXISTS ntee")
    pg.execute("CREATE TABLE ntee (ein text PRIMARY KEY, major text, category text)")
    t0, n = time.time(), 0
    cur = src.execute("SELECT ein, major, category FROM ntee")
    with pg.cursor().copy("COPY ntee (ein, major, category) FROM STDIN") as cp:
        while True:
            rows = cur.fetchmany(50_000)
            if not rows:
                break
            for r in rows:
                cp.write_row(r)
            n += len(rows)
    print(f"  copied {n:,} NTEE rows in {time.time() - t0:.0f}s")
    return n


# Population-scale rollups. The edge table is IMMUTABLE — 2022-2024 filings that will never
# change — so every "across the whole graph" answer can be computed once instead of on each ask.
# That is what makes the cheapest SKU viable: on a 1-vCore server the by-cause join over
# 7.8M x 1.4M rows takes ~280s live, and ~30ms from a rollup.
ROLLUPS = [
    ("agg_funder", """
        CREATE TABLE agg_funder AS
        SELECT funder_ein, MAX(funder_name) AS funder_name, SUM(amount) AS amount,
               COUNT(*) AS grants
        FROM grant_edges WHERE amount>0 GROUP BY funder_ein"""),
    ("agg_recipient", """
        CREATE TABLE agg_recipient AS
        SELECT recipient_name, SUM(amount) AS amount, COUNT(*) AS grants,
               COUNT(DISTINCT funder_ein) AS funders
        FROM grant_edges WHERE amount>0 GROUP BY recipient_name"""),
    ("agg_cause", """
        CREATE TABLE agg_cause AS
        SELECT n.major, MAX(n.category) AS category, SUM(g.amount) AS amount, COUNT(*) AS grants
        FROM grant_edges g JOIN ntee n ON g.recipient_ein=n.ein
        WHERE g.amount>0 GROUP BY n.major"""),
    ("agg_state", """
        CREATE TABLE agg_state AS
        SELECT funder_state, recipient_state, SUM(amount) AS amount, COUNT(*) AS grants
        FROM grant_edges WHERE amount>0 GROUP BY funder_state, recipient_state"""),
    ("agg_year", """
        CREATE TABLE agg_year AS
        SELECT tax_year, COUNT(*) AS grants, SUM(amount) AS amount
        FROM grant_edges WHERE amount>0 GROUP BY tax_year"""),
    ("agg_overview", """
        CREATE TABLE agg_overview AS
        SELECT COUNT(*) AS grants, SUM(amount) AS amount, AVG(amount) AS avg_amount,
               COUNT(DISTINCT funder_ein) AS funders, COUNT(DISTINCT recipient_name) AS recipients
        FROM grant_edges WHERE amount>0"""),
]

ROLLUP_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_af_amount ON agg_funder(amount DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ar_amount ON agg_recipient(amount DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ar_name ON agg_recipient USING gin (recipient_name gin_trgm_ops)",
]


def rollups(pg):
    for name, ddl in ROLLUPS:
        t = time.time()
        pg.execute(f"DROP TABLE IF EXISTS {name}")
        pg.execute(ddl)
        n = pg.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"  {name}: {n:,} rows in {time.time() - t:.0f}s", flush=True)
    for ddl in ROLLUP_INDEXES:
        pg.execute(ddl)
    pg.execute("ANALYZE")
    print("  rollup indexes + ANALYZE done")


def index(pg):
    try:
        pg.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as e:
        raise SystemExit(
            f"could not enable pg_trgm ({str(e)[:120]}).\n"
            "Azure Postgres allow-lists extensions per server; enable it once with:\n"
            "  az postgres flexible-server parameter set -g <rg> -s <server> \\\n"
            "      --name azure.extensions --value pg_trgm\n"
            "then re-run with --index-only. Without it the name lookups seq-scan 7.8M rows.")
    for name, ddl in INDEXES:
        t = time.time()
        pg.execute(ddl)
        print(f"  {name}: {time.time() - t:.0f}s", flush=True)
    t = time.time()
    pg.execute("ANALYZE grant_edges")
    print(f"  ANALYZE: {time.time() - t:.0f}s")


def verify(pg):
    """Every check must agree with the local sqlite, or the migration is not done."""
    src = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    ok = True
    print(f"\n{'check':26}{'sqlite':>20}{'postgres':>20}   match")
    for label, sql in CHECKS:
        a = src.execute(sql.replace("ROUND(SUM(amount))", "ROUND(SUM(amount))")).fetchone()[0]
        b = pg.execute(sql).fetchone()[0]
        a, b = (round(float(a)), round(float(b))) if isinstance(a, float) or isinstance(b, float) else (a, b)
        same = a == b
        ok = ok and same
        print(f"{label:26}{a:>20,}{b:>20,}   {'OK' if same else 'MISMATCH'}")

    # The one that actually exercises the case-insensitive substring path: sqlite LIKE ignores
    # case, Postgres LIKE does not, and 11% of these names are not uppercase. If the port used
    # LIKE instead of ILIKE, this is where it would silently come up short.
    name = "STANFORD"
    a = src.execute("SELECT COUNT(*) FROM grant_edges WHERE recipient_name LIKE ?",
                    (f"%{name}%",)).fetchone()[0]
    b = pg.execute("SELECT COUNT(*) FROM grant_edges WHERE recipient_name ILIKE %s",
                   (f"%{name}%",)).fetchone()[0]
    print(f"{'ILIKE %STANFORD%':26}{a:>20,}{b:>20,}   {'OK' if a == b else 'MISMATCH'}")
    ok = ok and a == b
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--ntee-only", action="store_true", help="load just the NTEE lookup")
    ap.add_argument("--rollups-only", action="store_true", help="rebuild the population rollups")
    ap.add_argument("--index-only", action="store_true",
                    help="rebuild indexes and verify against an already-loaded table")
    a = ap.parse_args(argv)
    pg = connect()
    print("connected:", pg.execute("SELECT version()").fetchone()[0][:60])
    if a.ntee_only:
        load_ntee(pg)
        return 0
    if a.rollups_only:
        rollups(pg)
        return 0
    if not (a.verify_only or a.index_only):
        load(pg)
        load_ntee(pg)
    if not a.verify_only:
        print("building indexes…")
        index(pg)
        print("building population rollups…")
        rollups(pg)
    ok = verify(pg)
    sz = pg.execute("SELECT pg_size_pretty(pg_total_relation_size('grant_edges'))").fetchone()[0]
    print(f"\ntable size in Postgres: {sz}")
    print("VERIFIED — safe to drop the local sqlite" if ok else "MISMATCH — do NOT drop the local copy")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
