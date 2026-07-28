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
import os, sqlite3, re

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("GRANTS_DB") or os.path.join(ROOT, "data", "990", "grants.sqlite")
SOURCE = "IRS Form 990 e-file grants (Schedule I + 990-PF, 2022-2024)"


def _conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def available():
    if not os.path.exists(DB):
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


def _forward_rows(c, where, arg, n):
    rows = c.execute(
        f"SELECT recipient_name, recipient_ein, SUM(amount) amt, COUNT(*) k "
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


def reverse(name, n=12):
    """Grants RECEIVED by `name` — its funders, biggest first. Prefers a recipient-EIN match
    (clean, from Schedule I); falls back to a recipient-name match (needed for 990-PF)."""
    ein, disp = _resolve(name)
    with _conn() as c:
        if ein:
            # EIN match (Schedule I) OR name match (990-PF carries no recipient EIN). Match the name
            # against the RAW query, not the resolved name, so a wrong resolver pick can't misdirect it.
            where = "(recipient_ein=? OR recipient_name LIKE ?)"
            arg = (ein, f"%{name.upper()}%")
            method = "EIN + name"
        else:
            where, arg, method, disp = "recipient_name LIKE ?", (f"%{name.upper()}%",), "name", name
        rows = c.execute(
            f"SELECT funder_name, funder_ein, SUM(amount) amt, COUNT(*) k "
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
        rows = c.execute(
            f"SELECT recipient_name, SUM(amount) amt, COUNT(DISTINCT funder_ein) fn "
            f"FROM grant_edges WHERE amount>0 AND recipient_name<>'' "
            f"GROUP BY recipient_name ORDER BY {order} {direction} LIMIT ?", (n,)).fetchall()
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
            row = c.execute("SELECT SUM(amount), COUNT(*) FROM grant_edges WHERE funder_state=? "
                            "AND recipient_state=? AND amount>0", (from_state, to_state)).fetchone()
            return {"direction": "geo_flow", "from_state": _ABBR.get(from_state, from_state),
                    "to_state": _ABBR.get(to_state, to_state),
                    "total_display": _disp(row[0] or 0), "grant_count": row[1] or 0, "source": SOURCE}
        col = "funder_state" if mode == "funders" else "recipient_state"
        rows = c.execute(
            f"SELECT {col}, SUM(amount) amt, COUNT(*) k FROM grant_edges "
            f"WHERE amount>0 AND {col} IS NOT NULL AND {col}<>'' "
            f"GROUP BY {col} ORDER BY amt {direction} LIMIT ?", (n,)).fetchall()
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
        n, tot, avg, nf, nr = c.execute(
            f"SELECT COUNT(*), SUM(amount), AVG(amount), COUNT(DISTINCT funder_ein), "
            f"COUNT(DISTINCT recipient_name) FROM grant_edges {where}", arg).fetchone()
        by_year = c.execute("SELECT tax_year, COUNT(*), SUM(amount) FROM grant_edges "
                            "WHERE amount>0 GROUP BY tax_year ORDER BY tax_year").fetchall()
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
            f"SELECT funder_name, SUM(amount) amt, COUNT(*) k FROM grant_edges WHERE amount>0 "
            f"GROUP BY funder_ein HAVING SUM(amount) {op} ? ORDER BY amt {direction} LIMIT ?",
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
        conn.execute("ATTACH DATABASE ? AS ntee", (f"file:{NTEE_DB}?mode=ro",))
        if major:
            row = conn.execute(
                "SELECT n.category, SUM(g.amount), COUNT(*) FROM grant_edges g JOIN ntee.ntee n "
                "ON g.recipient_ein=n.ein WHERE g.amount>0 AND n.major=?", (major,)).fetchone()
            return {"direction": "by_cause_one", "cause": (row[0] if row and row[0] else word),
                    "total_display": _disp((row[1] if row else 0) or 0),
                    "grant_count": (row[2] if row else 0) or 0, "coverage": _COVERAGE, "source": SOURCE}
        rows = conn.execute(
            "SELECT n.category, SUM(g.amount) amt, COUNT(*) k FROM grant_edges g JOIN ntee.ntee n "
            "ON g.recipient_ein=n.ein WHERE g.amount>0 GROUP BY n.major ORDER BY amt DESC LIMIT ?",
            (n,)).fetchall()
    rank = [{"label": r[0], "entity": f"cause/{r[0]}", "value": r[1], "value_display": _disp(r[1]),
             "grants": r[2]} for r in rows]
    return {"measure": "grant dollars by cause", "complete": True, "ranking": rank,
            "top": rank[0] if rank else None, "coverage": _COVERAGE, "source": SOURCE}


def top_grantmakers(n=10, ascending=False):
    """Population ranking: the biggest grantmakers by total dollars granted, 2022-2024."""
    order = "ASC" if ascending else "DESC"
    with _conn() as c:
        rows = c.execute(
            f"SELECT funder_name, SUM(amount) amt, COUNT(*) k FROM grant_edges "
            f"WHERE amount>0 GROUP BY funder_ein ORDER BY amt {order} LIMIT ?", (n,)).fetchall()
    rank = [{"label": r[0], "entity": f"grantmaker/{r[0]}", "value": r[1],
             "value_display": _disp(r[1]), "grants": r[2]} for r in rows]
    return {"measure": "total granted", "complete": True, "ranking": rank,
            "top": rank[0] if rank else None, "source": SOURCE}


if __name__ == "__main__":
    import sys, json
    if not available():
        raise SystemExit(f"no grant edges yet at {DB}")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    out = {"forward": lambda: forward(arg), "reverse": lambda: reverse(arg),
           "top": lambda: top_grantmakers()}[cmd]()
    print(json.dumps(out, indent=2, default=str))
