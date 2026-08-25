#!/usr/bin/env python3
"""The civil-society GRANT GRAPH — who funds whom — over IRS 990 e-file edges (2022-2024).

Each row in the edge table is one grant: funder -> recipient, amount, purpose, year, extracted
from Schedule I (public charities) and 990-PF Part XV (foundations) by tools/grants_etl.py. This
module traverses those edges in both directions:

    forward(name)  grants MADE by an org      — "who does the Ford Foundation fund?"
    reverse(name)  grants RECEIVED by an org  — "which foundations fund Stanford?"
    top_grantmakers(n)  the biggest funders   — a population ranking

Names are resolved to an EIN via the shared nonprofit resolver (ProPublica) and matched on EIN
when possible. The funder EIN is always present (it is the filer); recipient EINs are present for
Schedule I but not for 990-PF, so reverse also falls back to a name match and says which it used.
Credential-free and local: the edge table is a small sqlite file, so this needs no GCP project.
"""
import asyncio, os, sqlite3, re, decimal

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("GRANTS_DB") or os.path.join(ROOT, "data", "990", "grants.sqlite")
# Managed Postgres holding the same edge table. Set GRANTS_URL (or DATABASE_URL) and the queries
# below run against it instead of the local file; unset, everything falls back to sqlite.
URL = os.getenv("GRANTS_URL") or os.getenv("DATABASE_URL")
SOURCE = "IRS Form 990 e-file grants (Schedule I + 990-PF, 2022-2024)"


class _Rows:
    """Cursor wrapper that hands back plain Python numbers.

    Postgres returns `numeric` for SUM() over an integer column, which psycopg maps to Decimal —
    and Decimal is not JSON-serializable, so it reaches the caller as a 500 at response-encoding
    time, far from the query that produced it. sqlite has no such type, so every value here is
    expected to be int/float/str."""

    def __init__(self, cur):
        self._cur = cur

    @staticmethod
    def _plain(v):
        if isinstance(v, decimal.Decimal):
            return int(v) if v == v.to_integral_value() else float(v)
        return v

    def _row(self, r):
        return None if r is None else tuple(self._plain(v) for v in r)

    def fetchone(self):
        return self._row(self._cur.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _Pg:
    """The sqlite3 connection surface this module already uses, backed by Postgres.

    Every query here is portable aggregate SQL, so the port is two dialect fixes rather than a
    rewrite:

      `?` -> `%s`     placeholder style.
      LIKE -> ILIKE   sqlite's LIKE ignores ASCII case; Postgres's does not. The lookups here
                      match an upper-cased needle (`%STANFORD%`) against names that are only
                      ~89% upper-case in the IRS data, so a literal port would silently drop
                      about one match in nine — the kind of loss that reads as "no grants found"
                      rather than as an error.

    This class remains for offline commands. The server uses AsyncGrantPool below."""

    def __init__(self, url):
        self._url = url
        self._c = self._acquire(url)

    @classmethod
    def _acquire(cls, url):
        """Open an offline command connection; server connections come from AsyncGrantPool."""
        import psycopg
        return psycopg.connect(url, connect_timeout=20, autocommit=True)

    @staticmethod
    def _translate(sql):
        return re.sub(r"\bLIKE\b", "ILIKE", sql.replace("?", "%s"), flags=re.I)

    def execute(self, sql, params=()):
        try:
            cur = self._c.cursor()
            cur.execute(self._translate(sql), tuple(params))
        except Exception:
            # Reconnect once for an offline command after idle timeout, failover, or restart.
            self._c.close()
            self._c = self._acquire(self._url)
            cur = self._c.cursor()
            cur.execute(self._translate(sql), tuple(params))
        return _Rows(cur)

    def close(self):
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Sqlite:
    """Read-only sqlite context that actually closes its handle on exit."""
    def __init__(self, path):
        self._c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def __enter__(self):
        return self._c

    def __exit__(self, *exc):
        self._c.close()
        return False


def _broken(c):
    """True if the server has gone away on this handle."""
    try:
        from psycopg.pq import TransactionStatus
        return bool(c.closed) or c.info.transaction_status == TransactionStatus.UNKNOWN
    except Exception:
        return True


def _conn():
    if URL:
        return _Pg(URL)
    return _Sqlite(DB)


class AsyncGrantPool:
    """Application-owned psycopg 3 pool. Server mode deliberately has no SQLite fallback."""
    def __init__(self, url=None, min_size=1, max_size=10):
        url = url or os.getenv("GRANTS_URL") or os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError("async grant server mode requires GRANTS_URL or DATABASE_URL; "
                               "SQLite is offline/CLI-only")
        from psycopg_pool import AsyncConnectionPool
        self._pool_class = AsyncConnectionPool
        self._pool_args = (url, min_size, max_size)
        self._open_lock = asyncio.Lock()
        self.pool = None

    async def open(self, *, context=None):
        """Open on demand so an optional grants outage cannot block application startup."""
        if self.pool is not None:
            return self
        acquire = self._open_lock.acquire()
        await (context.wait(acquire) if context is not None else acquire)
        try:
            if self.pool is not None:
                return self
            url, min_size, max_size = self._pool_args
            pool = self._pool_class(url, min_size=min_size, max_size=max_size,
                                    open=False, kwargs={"autocommit": True})
            try:
                opening = pool.open(wait=True)
                await (context.wait(opening) if context is not None else opening)
            except BaseException:
                await pool.close()
                raise
            self.pool = pool
        finally:
            self._open_lock.release()
        return self

    async def close(self):
        if self.pool is not None:
            pool, self.pool = self.pool, None
            await pool.close()

    async def query(self, sql, params=(), *, context):
        await self.open(context=context)
        async def work():
            async with self.pool.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(_Pg._translate(sql), tuple(params))
                    rows = await cursor.fetchall()
                    return [tuple(_Rows._plain(value) for value in row) for row in rows]
        return await context.provider_call("grants", work)


async def _aq(sql, params=(), *, context):
    if context.grant_pool is None:
        raise RuntimeError("async grant access requires QueryContext.grant_pool")
    return await context.grant_pool.query(sql, params, context=context)


def available():
    if not URL and not os.path.exists(DB):
        return False
    try:
        with _conn() as c:
            return c.execute("SELECT 1 FROM grant_edges LIMIT 1").fetchone() is not None
    except Exception:
        return False


def _ein(v):
    d = re.sub(r"\D", "", str(v or ""))
    return d if len(d) == 9 else None


def _resolve(name):
    """(ein, display_name) for a name — reuse the nonprofit resolver; fall back to the raw name."""
    if _ein(name):
        return _ein(name), str(name)
    try:
        import nonprofit
        r = nonprofit.resolve(name)
        return _ein(r["ein"]), r["name"]
    except Exception:
        return None, str(name)


def _disp(v):
    return "${:,.0f}".format(v or 0)


# --- population-scale queries ---------------------------------------------------------------
# "Across the whole graph" answers (top grantmakers, biggest recipients, dollars by cause, by
# state, the overview) each scan all 7.8M edges. sqlite does that from a local file in seconds; a
# 1-vCore managed server takes 45s for a GROUP BY and ~280s for the cause join — a timeout, not a
# slow query. The edge table never changes, so tools/grants_to_postgres.py precomputes those
# aggregates once into agg_* tables and these read from them.
ROLLUPS = bool(URL)


def _rollup_or(sql_rollup, sql_scan):
    """The rollup query when the aggregates exist, else the full scan."""
    return sql_rollup if ROLLUPS else sql_scan



def _forward_rows(c, where, arg, n):
    rows = c.execute(
        f"SELECT recipient_name, MAX(recipient_ein), SUM(amount) amt, COUNT(*) k "
        f"FROM grant_edges WHERE {where} AND amount>0 GROUP BY recipient_name "
        f"ORDER BY amt DESC LIMIT ?", arg + (n,)).fetchall()
    tot = c.execute(f"SELECT SUM(amount), COUNT(*), COUNT(DISTINCT recipient_name) "
                    f"FROM grant_edges WHERE {where} AND amount>0", arg).fetchone()
    return rows, tot


def forward(name, n=12):
    """Grants MADE by `name` — its recipients, biggest first, plus totals. Tries an EIN match
    (from the resolver); if that finds nothing — a common miss when the resolver picks the wrong
    similarly-named org — falls back to a name match on the ORIGINAL query."""
    ein, disp = _resolve(name)
    with _conn() as c:
        rows, tot, method = ([], None, "name")
        if ein:
            rows, tot = _forward_rows(c, "funder_ein=?", (ein,), n)
            method = "EIN"
        if not rows:  # EIN missed or unresolved — match the filed funder name to the raw query
            rows, tot = _forward_rows(c, "funder_name LIKE ?", (f"%{name.upper()}%",), n)
            method, disp = "name", name  # resolver pick was wrong/none — show what the user asked
    if not rows:
        return {"direction": "grants_made", "funder": disp, "grant_count": 0,
                "note": "no grants found in the 2022-2024 IRS 990 e-file data for this funder",
                "source": SOURCE}
    recips = [{"recipient": r[0], "amount": r[2], "amount_display": _disp(r[2]), "grants": r[3]}
              for r in rows]
    return {"direction": "grants_made", "funder": disp, "matched_by": method,
            "total_granted_usd": tot[0], "total_granted_display": _disp(tot[0]),
            "grant_count": tot[1], "recipient_count": tot[2],
            "recipients": recips, "top": recips[0], "source": SOURCE}


def _ein_label(c, ein, fallback):
    """The recipient's own filed name for this EIN — the name attached to the most grant dollars in
    Schedule I. Keys the label on the EIN so we show "Stanford University", not whichever similarly
    named org (…Bookstore, …Hospital) the free-text resolver happened to pick."""
    row = c.execute("SELECT recipient_name FROM grant_edges WHERE recipient_ein=? AND recipient_name<>'' "
                    "GROUP BY recipient_name ORDER BY SUM(amount) DESC LIMIT 1", (ein,)).fetchone()
    return row[0] if row else fallback


def _dominant_recipient_ein(c, name):
    """The EIN of the biggest recipient (by grant dollars) whose filed name matches `name`. The grant
    data itself is the authoritative disambiguator: among all "STANFORD" recipients, the one drawing
    the most money is the real Stanford — more reliable than the generic name resolver, which can
    latch onto a tiny similarly-named org. Returns (ein, label) or (None, None) if no EIN'd match."""
    row = c.execute(
        "SELECT recipient_ein, MAX(recipient_name), SUM(amount) amt FROM grant_edges "
        "WHERE recipient_name LIKE ? AND recipient_ein<>'' AND amount>0 "
        "GROUP BY recipient_ein ORDER BY amt DESC LIMIT 1", (f"%{name.upper()}%",)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def reverse(name, n=12):
    """Grants RECEIVED by `name` — its funders, biggest first. Prefers a clean recipient-EIN match
    (from Schedule I); falls back to a recipient-name match only when no EIN'd match exists (needed
    for 990-PF, which carries no recipient EIN)."""
    ein, disp = _resolve(name)
    with _conn() as c:
        method, where, arg = "name", "recipient_name LIKE ?", (f"%{name.upper()}%",)
        # Prefer the dominant EIN'd recipient from the grant data; fall back to the resolver's EIN.
        dom_ein, dom_label = _dominant_recipient_ein(c, name)
        use_ein = dom_ein or (ein if ein and c.execute(
            "SELECT 1 FROM grant_edges WHERE recipient_ein=? AND amount>0 LIMIT 1", (ein,)).fetchone()
            else None)
        if use_ein:
            # EIN alone — an exact join key, so it can't conflate "Stanford University" with
            # "Stanford University Bookstore" the way a name LIKE would. Label from the EIN's own
            # filed name, not the resolver's guess.
            method, where, arg = "EIN", "recipient_ein=?", (use_ein,)
            disp = dom_label or _ein_label(c, use_ein, disp)
        else:
            disp = name  # no EIN'd match — show what the user asked
        rows = c.execute(
            f"SELECT MAX(funder_name), funder_ein, SUM(amount) amt, COUNT(*) k "
            f"FROM grant_edges WHERE {where} AND amount>0 GROUP BY funder_ein "
            f"ORDER BY amt DESC LIMIT ?", arg + (n,)).fetchall()
        tot = c.execute(f"SELECT SUM(amount), COUNT(DISTINCT funder_ein) "
                        f"FROM grant_edges WHERE {where} AND amount>0", arg).fetchone()
    if not rows:
        return {"direction": "funded_by", "recipient": disp, "funder_count": 0,
                "note": "no incoming grants found in the 2022-2024 IRS 990 e-file data for this recipient",
                "source": SOURCE}
    funders = [{"funder": r[0], "amount": r[2], "amount_display": _disp(r[2]), "grants": r[3]}
               for r in rows]
    return {"direction": "funded_by", "recipient": disp, "matched_by": method,
            "total_received_usd": tot[0], "total_received_display": _disp(tot[0]),
            "funder_count": tot[1], "funders": funders, "top": funders[0], "source": SOURCE}


def _funder_recipients(c, name):
    """{recipient_name: total} for grants MADE by `name` — EIN match, name fallback. Shared helper."""
    ein, disp = _resolve(name)
    if ein:
        rows = c.execute("SELECT recipient_name, SUM(amount) FROM grant_edges WHERE funder_ein=? "
                         "AND amount>0 GROUP BY recipient_name", (ein,)).fetchall()
        if rows:
            return {r[0]: r[1] for r in rows}, disp
    rows = c.execute("SELECT recipient_name, SUM(amount) FROM grant_edges WHERE funder_name LIKE ? "
                     "AND amount>0 GROUP BY recipient_name", (f"%{name.upper()}%",)).fetchall()
    return {r[0]: r[1] for r in rows}, (disp if ein and rows else name)


# --- graph patterns -----------------------------------------------------------------
def biggest_recipients(n=10, by="dollars", ascending=False):
    """Ranking of RECIPIENTS — by total grant dollars received (`dollars`) or by the number of
    distinct funders backing them (`funders`, an in-degree). The reverse of top_grantmakers."""
    order = "COUNT(DISTINCT funder_ein)" if by == "funders" else "SUM(amount)"
    direction = "ASC" if ascending else "DESC"
    with _conn() as c:
        rows = c.execute(_rollup_or(
            f"SELECT recipient_name, amount amt, funders fn FROM agg_recipient "
            f"WHERE recipient_name<>'' ORDER BY {'funders' if by == 'funders' else 'amount'} "
            f"{direction} LIMIT ?",
            f"SELECT recipient_name, SUM(amount) amt, COUNT(DISTINCT funder_ein) fn "
            f"FROM grant_edges WHERE amount>0 AND recipient_name<>'' "
            f"GROUP BY recipient_name ORDER BY {order} {direction} LIMIT ?"), (n,)).fetchall()
    rank = [{"label": r[0], "entity": f"recipient/{r[0]}",
             "value": (r[2] if by == "funders" else r[1]),
             "value_display": (f"{r[2]} funders" if by == "funders" else _disp(r[1])),
             "received_display": _disp(r[1]), "funders": r[2]} for r in rows]
    return {"measure": ("distinct funders" if by == "funders" else "total received"),
            "complete": True, "ranking": rank, "top": rank[0] if rank else None, "source": SOURCE}


def shared_grantees(name_a, name_b, n=20):
    """Organizations funded by BOTH named funders — a grant-graph intersection."""
    with _conn() as c:
        a, da = _funder_recipients(c, name_a)
        b, db = _funder_recipients(c, name_b)
    common = sorted(set(a) & set(b), key=lambda r: -(a[r] + b[r]))
    shared = [{"recipient": r, "from_a_display": _disp(a[r]), "from_b_display": _disp(b[r]),
               "combined": a[r] + b[r]} for r in common[:n]]
    return {"direction": "shared_grantees", "funder_a": da, "funder_b": db,
            "shared_count": len(common), "shared": shared,
            "a_recipient_count": len(a), "b_recipient_count": len(b), "source": SOURCE}


# --- geographic flows ---------------------------------------------------------------
STATES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
          "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
          "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
          "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
          "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
          "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
          "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
          "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
          "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
          "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
          "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
          "district of columbia": "DC", "washington dc": "DC", "washington, d.c.": "DC"}
_ABBR = {v: k.title() for k, v in STATES.items()}


def find_states(text):
    """State abbreviations named in `text`, in order of appearance — for 'from NY to California'
    style flows. Longest names are consumed first so 'west virginia' can't also match 'virginia'."""
    t = " " + re.sub(r"[^a-z ]", " ", (text or "").lower()) + " "  # punctuation -> space, so 'California?' matches
    hits = []
    for name in sorted(STATES, key=len, reverse=True):
        i = t.find(" " + name + " ")
        while i >= 0:
            hits.append((i, STATES[name]))
            t = t[:i + 1] + " " * len(name) + t[i + 1 + len(name):]  # consume the span
            i = t.find(" " + name + " ")
    seen, out = set(), []
    for pos, ab in sorted(hits):
        if ab not in seen:
            seen.add(ab); out.append(ab)
    return out


def geo(mode="recipients", from_state=None, to_state=None, n=12, ascending=False):
    """Grant money by place. mode='recipients' ranks receiving states, 'funders' ranks sending
    states, 'flow' totals the money from one state to another."""
    direction = "ASC" if ascending else "DESC"
    with _conn() as c:
        if mode == "flow" and from_state and to_state:
            row = c.execute(_rollup_or(
                "SELECT SUM(amount), SUM(grants) FROM agg_state WHERE funder_state=? "
                "AND recipient_state=?",
                "SELECT SUM(amount), COUNT(*) FROM grant_edges WHERE funder_state=? "
                "AND recipient_state=? AND amount>0"), (from_state, to_state)).fetchone()
            return {"direction": "geo_flow", "from_state": _ABBR.get(from_state, from_state),
                    "to_state": _ABBR.get(to_state, to_state),
                    "total_display": _disp(row[0] or 0), "grant_count": row[1] or 0, "source": SOURCE}
        col = "funder_state" if mode == "funders" else "recipient_state"
        rows = c.execute(_rollup_or(
            f"SELECT {col}, SUM(amount) amt, SUM(grants) k FROM agg_state "
            f"WHERE {col} IS NOT NULL AND {col}<>'' GROUP BY {col} ORDER BY amt {direction} LIMIT ?",
            f"SELECT {col}, SUM(amount) amt, COUNT(*) k FROM grant_edges "
            f"WHERE amount>0 AND {col} IS NOT NULL AND {col}<>'' "
            f"GROUP BY {col} ORDER BY amt {direction} LIMIT ?"), (n,)).fetchall()
    rank = [{"label": _ABBR.get(r[0], r[0]), "entity": f"state/{r[0]}", "value": r[1],
             "value_display": _disp(r[1]), "grants": r[2]} for r in rows]
    verb = "sent" if mode == "funders" else "received"
    return {"measure": f"total grant dollars {verb}", "complete": True, "ranking": rank,
            "top": rank[0] if rank else None, "source": SOURCE}


# --- aggregates & filtered subsets --------------------------------------------------
def overview(year=None):
    """Headline numbers for the whole grant graph (optionally one tax year): counts, totals,
    average grant size, distinct funders and recipients, plus a per-year breakdown."""
    where, arg = ("WHERE amount>0", ())
    if year:
        where, arg = ("WHERE amount>0 AND tax_year=?", (year,))
    with _conn() as c:
        if ROLLUPS and not year:       # the whole-graph figures are precomputed; a single year is not
            n, tot, avg, nf, nr = c.execute(
                "SELECT grants, amount, avg_amount, funders, recipients FROM agg_overview").fetchone()
        else:
            n, tot, avg, nf, nr = c.execute(
                f"SELECT COUNT(*), SUM(amount), AVG(amount), COUNT(DISTINCT funder_ein), "
                f"COUNT(DISTINCT recipient_name) FROM grant_edges {where}", arg).fetchone()
        by_year = c.execute(_rollup_or(
            "SELECT tax_year, grants, amount FROM agg_year ORDER BY tax_year",
            "SELECT tax_year, COUNT(*), SUM(amount) FROM grant_edges "
            "WHERE amount>0 GROUP BY tax_year ORDER BY tax_year")).fetchall()
    return {"direction": "overview", "scope": (f"tax year {year}" if year else "2022-2024 filings"),
            "grant_count": n or 0, "total_display": _disp(tot or 0), "avg_grant_display": _disp(avg or 0),
            "funder_count": nf or 0, "recipient_count": nr or 0,
            "by_year": [{"year": y, "grants": k, "total_display": _disp(t or 0)} for y, k, t in by_year],
            "source": SOURCE}


def funders_above(threshold, n=60, ascending=False):
    """Filtered subset: funders whose TOTAL granted crosses a dollar threshold (a HAVING)."""
    direction = "ASC" if ascending else "DESC"
    op = "<" if ascending else ">"
    with _conn() as c:
        rows = c.execute(
            _rollup_or(
                f"SELECT funder_name, amount amt, grants k FROM agg_funder WHERE amount {op} ? "
                f"ORDER BY amt {direction} LIMIT ?",
                f"SELECT MAX(funder_name), SUM(amount) amt, COUNT(*) k FROM grant_edges WHERE amount>0 "
                f"GROUP BY funder_ein HAVING SUM(amount) {op} ? ORDER BY amt {direction} LIMIT ?"),
            (float(threshold), n)).fetchall()
    rank = [{"label": r[0], "entity": f"grantmaker/{r[0]}", "value": r[1],
             "value_display": _disp(r[1]), "grants": r[2]} for r in rows]
    return {"measure": "total granted", "matches": len(rank),
            "threshold_display": f"{'under' if ascending else 'over'} {_disp(float(threshold))}",
            "complete": True, "ranking": rank, "source": SOURCE}


# --- thematic / by-cause (joins the BMF NTEE lookup) --------------------------------
NTEE_DB = os.path.join(ROOT, "data", "990", "ntee.sqlite")
CAUSE_KEYWORDS = {  # query word -> NTEE major-group letter
    "education": "B", "school": "B", "scholarship": "B", "health": "E", "healthcare": "E",
    "hospital": "E", "environment": "C", "environmental": "C", "climate": "C", "conservation": "C",
    "arts": "A", "culture": "A", "cultural": "A", "museum": "A", "housing": "L", "shelter": "L",
    "homeless": "L", "food": "K", "hunger": "K", "nutrition": "K", "agriculture": "K",
    "human services": "P", "social services": "P", "religion": "X", "religious": "X", "faith": "X",
    "church": "X", "international": "Q", "foreign": "Q", "global": "Q", "animal": "D", "animals": "D",
    "wildlife": "D", "youth": "O", "children": "O", "research": "H", "medical research": "H",
    "civil rights": "R", "advocacy": "R", "mental health": "F", "disease": "G", "employment": "J",
    "jobs": "J", "crime": "I", "legal": "I", "recreation": "N", "sports": "N", "science": "U",
    "community": "S", "disaster": "M", "public safety": "M", "philanthropy": "T",
}
_COVERAGE = "charity-to-charity grants whose recipient EIN is reported (Schedule I); 990-PF " \
            "foundation grants carry no recipient EIN and are not classified by cause"


def cause_of(text):
    """(major-letter, matched-word) for a cause named in `text`, else (None, None). Longest first."""
    t = (text or "").lower()
    for word in sorted(CAUSE_KEYWORDS, key=len, reverse=True):
        if word in t:
            return CAUSE_KEYWORDS[word], word
    return None, None


def grants_by_cause(cause=None, n=15):
    """Grant dollars grouped by recipient CAUSE (NTEE major group), or the total to ONE cause —
    joining recipient_ein to the BMF NTEE lookup. Schedule I slice only (see coverage)."""
    major, word = cause_of(cause) if cause else (None, None)
    with _conn() as conn:
        # The NTEE lookup is a SEPARATE sqlite file, so sqlite has to ATTACH it before it can be
        # joined. Postgres has no ATTACH — both tables live in the one database — so the only
        # difference is what the table is called.
        nt = "ntee"
        if not ROLLUPS:                    # only the live join needs the lookup attached
            conn.execute("ATTACH DATABASE ? AS ntee", (f"file:{NTEE_DB}?mode=ro",))
            nt = "ntee.ntee"
        if major:
            row = conn.execute(_rollup_or(
                "SELECT category, amount, grants FROM agg_cause WHERE major=?",
                f"SELECT MAX(n.category), SUM(g.amount), COUNT(*) FROM grant_edges g JOIN {nt} n "
                f"ON g.recipient_ein=n.ein WHERE g.amount>0 AND n.major=?"), (major,)).fetchone()
            return {"direction": "by_cause_one", "cause": (row[0] if row and row[0] else word),
                    "total_display": _disp((row[1] if row else 0) or 0),
                    "grant_count": (row[2] if row else 0) or 0, "coverage": _COVERAGE, "source": SOURCE}
        rows = conn.execute(_rollup_or(
            "SELECT category, amount amt, grants k FROM agg_cause ORDER BY amt DESC LIMIT ?",
            f"SELECT MAX(n.category), SUM(g.amount) amt, COUNT(*) k FROM grant_edges g JOIN {nt} n "
            f"ON g.recipient_ein=n.ein WHERE g.amount>0 GROUP BY n.major ORDER BY amt DESC LIMIT ?"),
            (n,)).fetchall()
    rank = [{"label": r[0], "entity": f"cause/{r[0]}", "value": r[1], "value_display": _disp(r[1]),
             "grants": r[2]} for r in rows]
    return {"measure": "grant dollars by cause", "complete": True, "ranking": rank,
            "top": rank[0] if rank else None, "coverage": _COVERAGE, "source": SOURCE}


def top_grantmakers(n=10, ascending=False):
    """Population ranking: the biggest grantmakers by total dollars granted, 2022-2024."""
    order = "ASC" if ascending else "DESC"
    with _conn() as c:
        rows = c.execute(_rollup_or(
            f"SELECT funder_name, amount amt, grants k FROM agg_funder ORDER BY amt {order} LIMIT ?",
            f"SELECT MAX(funder_name), SUM(amount) amt, COUNT(*) k FROM grant_edges "
            f"WHERE amount>0 GROUP BY funder_ein ORDER BY amt {order} LIMIT ?"), (n,)).fetchall()
    rank = [{"label": r[0], "entity": f"grantmaker/{r[0]}", "value": r[1],
             "value_display": _disp(r[1]), "grants": r[2]} for r in rows]
    return {"measure": "total granted", "complete": True, "ranking": rank,
            "top": rank[0] if rank else None, "source": SOURCE}


# --- asynchronous server path ---------------------------------------------------------------
async def _resolve_async(name, context):
    if _ein(name):
        return _ein(name), str(name)
    try:
        import nonprofit
        result = await nonprofit.resolve_async(name, context=context)
        return _ein(result["ein"]), result["name"]
    except Exception:
        return None, str(name)


async def forward_async(name, n=12, *, context):
    ein, display = await _resolve_async(name, context)
    rows, total, method = [], [], "name"
    if ein:
        rows = await _aq(
            "SELECT recipient_name, MAX(recipient_ein), SUM(amount) amt, COUNT(*) k "
            "FROM grant_edges WHERE funder_ein=? AND amount>0 GROUP BY recipient_name "
            "ORDER BY amt DESC LIMIT ?", (ein, n), context=context)
        if rows:
            total = await _aq("SELECT SUM(amount), COUNT(*), COUNT(DISTINCT recipient_name) "
                              "FROM grant_edges WHERE funder_ein=? AND amount>0", (ein,), context=context)
            method = "EIN"
    if not rows:
        needle = f"%{name.upper()}%"
        rows = await _aq(
            "SELECT recipient_name, MAX(recipient_ein), SUM(amount) amt, COUNT(*) k "
            "FROM grant_edges WHERE funder_name LIKE ? AND amount>0 GROUP BY recipient_name "
            "ORDER BY amt DESC LIMIT ?", (needle, n), context=context)
        total = await _aq("SELECT SUM(amount), COUNT(*), COUNT(DISTINCT recipient_name) "
                          "FROM grant_edges WHERE funder_name LIKE ? AND amount>0", (needle,), context=context)
        display = name
    if not rows:
        return {"direction": "grants_made", "funder": display, "grant_count": 0,
                "note": "no grants found in the 2022-2024 IRS 990 e-file data for this funder",
                "source": SOURCE}
    recipients = [{"recipient": row[0], "amount": row[2], "amount_display": _disp(row[2]),
                   "grants": row[3]} for row in rows]
    totals = total[0]
    return {"direction": "grants_made", "funder": display, "matched_by": method,
            "total_granted_usd": totals[0], "total_granted_display": _disp(totals[0]),
            "grant_count": totals[1], "recipient_count": totals[2], "recipients": recipients,
            "top": recipients[0], "source": SOURCE}


async def reverse_async(name, n=12, *, context):
    ein, display = await _resolve_async(name, context)
    needle = f"%{name.upper()}%"
    dominant = await _aq(
        "SELECT recipient_ein, MAX(recipient_name), SUM(amount) amt FROM grant_edges "
        "WHERE recipient_name LIKE ? AND recipient_ein<>'' AND amount>0 "
        "GROUP BY recipient_ein ORDER BY amt DESC LIMIT 1", (needle,), context=context)
    use_ein = dominant[0][0] if dominant else None
    if not use_ein and ein:
        exists = await _aq("SELECT 1 FROM grant_edges WHERE recipient_ein=? AND amount>0 LIMIT 1",
                           (ein,), context=context)
        use_ein = ein if exists else None
    if use_ein:
        where, params, method = "recipient_ein=?", (use_ein,), "EIN"
        if dominant:
            display = dominant[0][1]
    else:
        where, params, method, display = "recipient_name LIKE ?", (needle,), "name", name
    rows = await _aq(
        f"SELECT MAX(funder_name), funder_ein, SUM(amount) amt, COUNT(*) k FROM grant_edges "
        f"WHERE {where} AND amount>0 GROUP BY funder_ein ORDER BY amt DESC LIMIT ?",
        params + (n,), context=context)
    total = await _aq(f"SELECT SUM(amount), COUNT(DISTINCT funder_ein) FROM grant_edges "
                      f"WHERE {where} AND amount>0", params, context=context)
    if not rows:
        return {"direction": "funded_by", "recipient": display, "funder_count": 0,
                "note": "no incoming grants found in the 2022-2024 IRS 990 e-file data for this recipient",
                "source": SOURCE}
    funders = [{"funder": row[0], "amount": row[2], "amount_display": _disp(row[2]),
                "grants": row[3]} for row in rows]
    totals = total[0]
    return {"direction": "funded_by", "recipient": display, "matched_by": method,
            "total_received_usd": totals[0], "total_received_display": _disp(totals[0]),
            "funder_count": totals[1], "funders": funders, "top": funders[0], "source": SOURCE}


async def top_grantmakers_async(n=10, ascending=False, *, context):
    order = "ASC" if ascending else "DESC"
    rows = await _aq(
        _rollup_or(f"SELECT funder_name, amount amt, grants k FROM agg_funder "
                   f"ORDER BY amt {order} LIMIT ?",
                   f"SELECT MAX(funder_name), SUM(amount) amt, COUNT(*) k FROM grant_edges "
                   f"WHERE amount>0 GROUP BY funder_ein ORDER BY amt {order} LIMIT ?"),
        (n,), context=context)
    ranking = [{"label": row[0], "entity": f"grantmaker/{row[0]}", "value": row[1],
                "value_display": _disp(row[1]), "grants": row[2]} for row in rows]
    return {"measure": "total granted", "complete": True, "ranking": ranking,
            "top": ranking[0] if ranking else None, "source": SOURCE}


async def biggest_recipients_async(n=10, by="dollars", ascending=False, *, context):
    order = "COUNT(DISTINCT funder_ein)" if by == "funders" else "SUM(amount)"
    direction = "ASC" if ascending else "DESC"
    rows = await _aq(_rollup_or(
        f"SELECT recipient_name, amount amt, funders fn FROM agg_recipient WHERE recipient_name<>'' "
        f"ORDER BY {'funders' if by == 'funders' else 'amount'} {direction} LIMIT ?",
        f"SELECT recipient_name, SUM(amount) amt, COUNT(DISTINCT funder_ein) fn FROM grant_edges "
        f"WHERE amount>0 AND recipient_name<>'' GROUP BY recipient_name "
        f"ORDER BY {order} {direction} LIMIT ?"), (n,), context=context)
    ranking = [{"label": row[0], "entity": f"recipient/{row[0]}",
                "value": row[2] if by == "funders" else row[1],
                "value_display": f"{row[2]} funders" if by == "funders" else _disp(row[1]),
                "received_display": _disp(row[1]), "funders": row[2]} for row in rows]
    return {"measure": "distinct funders" if by == "funders" else "total received",
            "complete": True, "ranking": ranking, "top": ranking[0] if ranking else None,
            "source": SOURCE}


async def funders_above_async(threshold, n=60, ascending=False, *, context):
    direction, operator = ("ASC", "<") if ascending else ("DESC", ">")
    rows = await _aq(_rollup_or(
        f"SELECT funder_name, amount amt, grants k FROM agg_funder WHERE amount {operator} ? "
        f"ORDER BY amt {direction} LIMIT ?",
        f"SELECT MAX(funder_name), SUM(amount) amt, COUNT(*) k FROM grant_edges WHERE amount>0 "
        f"GROUP BY funder_ein HAVING SUM(amount) {operator} ? ORDER BY amt {direction} LIMIT ?"),
        (float(threshold), n), context=context)
    ranking = [{"label": row[0], "entity": f"grantmaker/{row[0]}", "value": row[1],
                "value_display": _disp(row[1]), "grants": row[2]} for row in rows]
    return {"measure": "total granted", "matches": len(ranking),
            "threshold_display": f"{'under' if ascending else 'over'} {_disp(float(threshold))}",
            "complete": True, "ranking": ranking, "source": SOURCE}


async def overview_async(year=None, *, context):
    if ROLLUPS and not year:
        summary = (await _aq(
            "SELECT grants, amount, avg_amount, funders, recipients FROM agg_overview",
            context=context))[0]
    else:
        where, params = ("WHERE amount>0 AND tax_year=?", (year,)) if year else ("WHERE amount>0", ())
        summary = (await _aq(
            "SELECT COUNT(*), SUM(amount), AVG(amount), COUNT(DISTINCT funder_ein), "
            f"COUNT(DISTINCT recipient_name) FROM grant_edges {where}", params, context=context))[0]
    years = await _aq(_rollup_or(
        "SELECT tax_year, grants, amount FROM agg_year ORDER BY tax_year",
        "SELECT tax_year, COUNT(*), SUM(amount) FROM grant_edges WHERE amount>0 "
        "GROUP BY tax_year ORDER BY tax_year"), context=context)
    count, total, average, funders, recipients = summary
    return {"direction": "overview", "scope": f"tax year {year}" if year else "2022-2024 filings",
            "grant_count": count or 0, "total_display": _disp(total or 0),
            "avg_grant_display": _disp(average or 0), "funder_count": funders or 0,
            "recipient_count": recipients or 0,
            "by_year": [{"year": y, "grants": n, "total_display": _disp(value or 0)}
                        for y, n, value in years], "source": SOURCE}


async def geo_async(mode="recipients", from_state=None, to_state=None, n=12, ascending=False,
                    *, context):
    direction = "ASC" if ascending else "DESC"
    if mode == "flow" and from_state and to_state:
        rows = await _aq(_rollup_or(
            "SELECT SUM(amount), SUM(grants) FROM agg_state WHERE funder_state=? AND recipient_state=?",
            "SELECT SUM(amount), COUNT(*) FROM grant_edges WHERE funder_state=? "
            "AND recipient_state=? AND amount>0"), (from_state, to_state), context=context)
        row = rows[0]
        return {"direction": "geo_flow", "from_state": _ABBR.get(from_state, from_state),
                "to_state": _ABBR.get(to_state, to_state), "total_display": _disp(row[0] or 0),
                "grant_count": row[1] or 0, "source": SOURCE}
    column = "funder_state" if mode == "funders" else "recipient_state"
    rows = await _aq(_rollup_or(
        f"SELECT {column}, SUM(amount) amt, SUM(grants) k FROM agg_state WHERE {column} IS NOT NULL "
        f"AND {column}<>'' GROUP BY {column} ORDER BY amt {direction} LIMIT ?",
        f"SELECT {column}, SUM(amount) amt, COUNT(*) k FROM grant_edges WHERE amount>0 "
        f"AND {column} IS NOT NULL AND {column}<>'' GROUP BY {column} "
        f"ORDER BY amt {direction} LIMIT ?"), (n,), context=context)
    ranking = [{"label": _ABBR.get(row[0], row[0]), "entity": f"state/{row[0]}",
                "value": row[1], "value_display": _disp(row[1]), "grants": row[2]} for row in rows]
    return {"measure": f"total grant dollars {'sent' if mode == 'funders' else 'received'}",
            "complete": True, "ranking": ranking, "top": ranking[0] if ranking else None,
            "source": SOURCE}


async def grants_by_cause_async(cause=None, n=15, *, context):
    major, word = cause_of(cause) if cause else (None, None)
    if not ROLLUPS:
        raise RuntimeError("async grant server mode requires Postgres rollup tables")
    if major:
        rows = await _aq("SELECT category, amount, grants FROM agg_cause WHERE major=?",
                         (major,), context=context)
        row = rows[0] if rows else None
        return {"direction": "by_cause_one", "cause": row[0] if row and row[0] else word,
                "total_display": _disp((row[1] if row else 0) or 0),
                "grant_count": (row[2] if row else 0) or 0, "coverage": _COVERAGE,
                "source": SOURCE}
    rows = await _aq("SELECT category, amount amt, grants k FROM agg_cause "
                     "ORDER BY amt DESC LIMIT ?", (n,), context=context)
    ranking = [{"label": row[0], "entity": f"cause/{row[0]}", "value": row[1],
                "value_display": _disp(row[1]), "grants": row[2]} for row in rows]
    return {"measure": "grant dollars by cause", "complete": True, "ranking": ranking,
            "top": ranking[0] if ranking else None, "coverage": _COVERAGE, "source": SOURCE}


async def _funder_recipients_async(name, context):
    ein, display = await _resolve_async(name, context)
    rows = []
    if ein:
        rows = await _aq("SELECT recipient_name, SUM(amount) FROM grant_edges WHERE funder_ein=? "
                         "AND amount>0 GROUP BY recipient_name", (ein,), context=context)
    if rows:
        return {row[0]: row[1] for row in rows}, display
    rows = await _aq("SELECT recipient_name, SUM(amount) FROM grant_edges WHERE funder_name LIKE ? "
                     "AND amount>0 GROUP BY recipient_name", (f"%{name.upper()}%",), context=context)
    return {row[0]: row[1] for row in rows}, name


async def shared_grantees_async(name_a, name_b, n=20, *, context):
    first, first_name = await _funder_recipients_async(name_a, context)
    second, second_name = await _funder_recipients_async(name_b, context)
    common = sorted(set(first) & set(second), key=lambda recipient: -(first[recipient] + second[recipient]))
    shared = [{"recipient": recipient, "from_a_display": _disp(first[recipient]),
               "from_b_display": _disp(second[recipient]),
               "combined": first[recipient] + second[recipient]} for recipient in common[:n]]
    return {"direction": "shared_grantees", "funder_a": first_name, "funder_b": second_name,
            "shared_count": len(common), "shared": shared, "a_recipient_count": len(first),
            "b_recipient_count": len(second), "source": SOURCE}


ASYNC_OPERATIONS = {
    "forward": forward_async, "reverse": reverse_async,
    "top_grantmakers": top_grantmakers_async, "biggest_recipients": biggest_recipients_async,
    "funders_above": funders_above_async, "overview": overview_async, "geo": geo_async,
    "grants_by_cause": grants_by_cause_async, "shared_grantees": shared_grantees_async,
}


async def execute_async(operation, *args, context, **kwargs):
    try:
        function = ASYNC_OPERATIONS[operation]
    except KeyError:
        raise NotImplementedError(f"async grant operation {operation!r} is not implemented")
    return await function(*args, context=context, **kwargs)


if __name__ == "__main__":
    import sys, json
    if not available():
        raise SystemExit(f"no grant edges yet at {DB}")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    out = {"forward": lambda: forward(arg), "reverse": lambda: reverse(arg),
           "top": lambda: top_grantmakers()}[cmd]()
    print(json.dumps(out, indent=2, default=str))
