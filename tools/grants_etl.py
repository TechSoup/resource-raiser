#!/usr/bin/env python3
"""Extract the civil-society GRANT GRAPH from IRS 990 e-file XML.

Every 990 filing that makes grants lists them: public charities in Schedule I, private
foundations in Form 990-PF Part XV. Each listed grant is one EDGE — funder -> recipient,
with an amount, a purpose and a year. This turns tens of GB of per-filing XML (throwaway)
into a compact edge table (kept) that answers who-funds-whom in both directions.

Two record shapes, one edge each:

  Schedule I  <RecipientTable>              org grant, recipient EIN present  (clean reverse lookup)
    RecipientBusinessName/BusinessNameLine1Txt, RecipientEIN, CashGrantAmt, PurposeOfGrantTxt, USAddress

  990-PF      <GrantOrContributionPdDurYrGrp>   foundation grant, NO recipient EIN (name+state only)
    RecipientBusinessName/BusinessNameLine1Txt, RecipientUSAddress, GrantOrContributionPurposeTxt, Amt

Filer (funder) EIN/name/state and TaxYr come from the return header. Individual grants
(RecipientPersonNm, e.g. scholarships) are skipped — the graph is org -> org.

Usage:
    python tools/grants_etl.py parse <dir-of-xml>   [db]     # parse an already-unzipped dir
    python tools/grants_etl.py zip   <file.zip>      [db]     # unzip in a temp dir, parse, clean up
    python tools/grants_etl.py stats                 [db]
"""
import os, re, sys, sqlite3, zipfile, tempfile, shutil, glob
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "990", "grants.sqlite")

DDL = """
CREATE TABLE IF NOT EXISTS grant_edges (
  funder_ein TEXT, funder_name TEXT, funder_state TEXT,
  recipient_ein TEXT, recipient_name TEXT, recipient_state TEXT,
  amount REAL, purpose TEXT, tax_year INTEGER, form TEXT
);
CREATE INDEX IF NOT EXISTS idx_ge_funder     ON grant_edges(funder_ein);
CREATE INDEX IF NOT EXISTS idx_ge_recip_ein  ON grant_edges(recipient_ein);
CREATE INDEX IF NOT EXISTS idx_ge_funder_nm  ON grant_edges(funder_name);
CREATE INDEX IF NOT EXISTS idx_ge_recip_nm   ON grant_edges(recipient_name);
CREATE TABLE IF NOT EXISTS done_files (name TEXT PRIMARY KEY, edges INTEGER);
"""


def _local(tag):
    return tag.rsplit("}", 1)[-1]  # strip {http://www.irs.gov/efile} namespace


def _first(el, path):
    """First descendant whose local tag-name matches the last path segment under the trail."""
    segs = path.split("/")
    cur = [el]
    for s in segs:
        nxt = []
        for c in cur:
            nxt += [ch for ch in c if _local(ch.tag) == s]
        cur = nxt
        if not cur:
            return None
    return cur[0]


def _text(el, path):
    n = _first(el, path)
    return n.text.strip() if (n is not None and n.text) else None


def _ein(v):
    if not v:
        return None
    d = re.sub(r"\D", "", v)
    return d if len(d) == 9 else None


def _amt(v):
    try:
        return float(re.sub(r"[^\d.\-]", "", v)) if v else None
    except ValueError:
        return None


def _index(root):
    """Map local-tag-name -> list of elements, once, so lookups don't rescan the tree."""
    idx = {}
    for el in root.iter():
        idx.setdefault(_local(el.tag), []).append(el)
    return idx


def parse_file(path):
    """Yield grant edges (dicts) from one filing. Empty for non-grant returns."""
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return
    idx = _index(root)
    filer = (idx.get("Filer") or [None])[0]
    if filer is None:
        return
    f_ein = _ein(_text(filer, "EIN"))
    f_name = _text(filer, "BusinessName/BusinessNameLine1Txt")
    f_state = _text(filer, "USAddress/StateAbbreviationCd")
    yr = (idx.get("TaxYr") or [None])[0]
    tax_year = int(yr.text) if (yr is not None and yr.text and yr.text.isdigit()) else None
    rtype = (idx.get("ReturnTypeCd") or [None])[0]
    form = rtype.text if (rtype is not None and rtype.text) else None

    # Schedule I — public-charity grants to organizations (recipient EIN present)
    for g in idx.get("RecipientTable", []):
        name = _text(g, "RecipientBusinessName/BusinessNameLine1Txt")
        if not name:
            continue  # individual/other rows without an org name
        yield {"funder_ein": f_ein, "funder_name": f_name, "funder_state": f_state,
               "recipient_ein": _ein(_text(g, "RecipientEIN")), "recipient_name": name,
               "recipient_state": _text(g, "USAddress/StateAbbreviationCd"),
               "amount": _amt(_text(g, "CashGrantAmt")),
               "purpose": _text(g, "PurposeOfGrantTxt"),
               "tax_year": tax_year, "form": form or "990"}

    # 990-PF — foundation grants paid (recipient name + state, no EIN)
    for g in idx.get("GrantOrContributionPdDurYrGrp", []):
        name = _text(g, "RecipientBusinessName/BusinessNameLine1Txt")
        if not name:
            continue  # RecipientPersonNm individual grants — skipped (org->org graph)
        yield {"funder_ein": f_ein, "funder_name": f_name, "funder_state": f_state,
               "recipient_ein": None, "recipient_name": name,
               "recipient_state": _text(g, "RecipientUSAddress/StateAbbreviationCd"),
               "amount": _amt(_text(g, "Amt")),
               "purpose": _text(g, "GrantOrContributionPurposeTxt"),
               "tax_year": tax_year, "form": "990PF"}


COLS = ["funder_ein", "funder_name", "funder_state", "recipient_ein", "recipient_name",
        "recipient_state", "amount", "purpose", "tax_year", "form"]


def _conn(db):
    os.makedirs(os.path.dirname(db), exist_ok=True)
    c = sqlite3.connect(db, timeout=60)
    c.executescript(DDL)
    return c


def parse_dir(dirpath, db=DB, tag=None):
    c = _conn(db)
    tag = tag or os.path.basename(dirpath.rstrip("/"))
    if c.execute("SELECT 1 FROM done_files WHERE name=?", (tag,)).fetchone():
        print(f"  {tag}: already done, skipping")
        return 0
    n = 0
    buf = []
    for p in glob.iglob(os.path.join(dirpath, "**", "*.xml"), recursive=True):
        for e in parse_file(p):
            buf.append([e[k] for k in COLS])
            n += 1
        if len(buf) >= 5000:
            c.executemany(f"INSERT INTO grant_edges ({','.join(COLS)}) VALUES ({','.join('?'*len(COLS))})", buf)
            buf.clear()
    if buf:
        c.executemany(f"INSERT INTO grant_edges ({','.join(COLS)}) VALUES ({','.join('?'*len(COLS))})", buf)
    c.execute("INSERT OR REPLACE INTO done_files (name, edges) VALUES (?,?)", (tag, n))
    c.commit()
    c.close()
    print(f"  {tag}: {n:,} edges")
    return n


def parse_zip(zippath, db=DB):
    tag = os.path.basename(zippath).replace(".zip", "")
    c = _conn(db)
    if c.execute("SELECT 1 FROM done_files WHERE name=?", (tag,)).fetchone():
        print(f"  {tag}: already done, skipping"); c.close(); return 0
    c.close()
    tmp = tempfile.mkdtemp(prefix="g990_")
    try:
        with zipfile.ZipFile(zippath) as z:
            z.extractall(tmp)
        return parse_dir(tmp, db, tag=tag)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stats(db=DB):
    c = _conn(db)
    n = c.execute("SELECT COUNT(*) FROM grant_edges").fetchone()[0]
    yrs = c.execute("SELECT tax_year, COUNT(*) FROM grant_edges GROUP BY tax_year ORDER BY tax_year").fetchall()
    forms = c.execute("SELECT form, COUNT(*) FROM grant_edges GROUP BY form").fetchall()
    withein = c.execute("SELECT COUNT(*) FROM grant_edges WHERE recipient_ein IS NOT NULL").fetchone()[0]
    print(f"{n:,} grant edges  ({withein:,} with recipient EIN)")
    print("by year:", dict(yrs))
    print("by form:", dict(forms))
    print("done files:", c.execute("SELECT COUNT(*) FROM done_files").fetchone()[0])
    c.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    db = sys.argv[3] if len(sys.argv) > 3 else DB
    if cmd == "parse":
        parse_dir(arg, db)
    elif cmd == "zip":
        parse_zip(arg, db)
    elif cmd == "stats":
        stats(arg or DB)
    else:
        print(__doc__)
