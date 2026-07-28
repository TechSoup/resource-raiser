#!/usr/bin/env python3
"""Load an EIN -> NTEE cause lookup from the IRS Exempt Organizations Business Master File.

The grant edges name recipients, but not what they DO. The BMF classifies every registered
tax-exempt org by NTEE code, whose first letter is the major cause group (B=Education, E=Health,
…). Loading EIN -> major-group lets the grant graph answer "how much grant money goes to education?"
by joining recipient_ein to this table. Recipient EINs are present on Schedule I edges (public-
charity grants); 990-PF foundation grants carry no recipient EIN, so thematic coverage is the
Schedule I slice — stated honestly at query time.

Loads into the SAME sqlite as the grant edges, so the join is one local SQL query.
Usage: python tools/bmf_ntee.py            # download eo1-4 and build the ntee table
"""
import os, sys, csv, sqlite3, urllib.request, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Its own file (not grants.sqlite) so loading it never contends with the grant-edge writer;
# grants.py ATTACHes it for the cause join.
DB = os.path.join(ROOT, "data", "990", "ntee.sqlite")
FILES = ["eo1", "eo2", "eo3", "eo4"]
BASE = "https://www.irs.gov/pub/irs-soi/{}.csv"

# NTEE major group (first letter of NTEE_CD) -> human cause name
MAJOR = {
    "A": "Arts, Culture & Humanities", "B": "Education", "C": "Environment", "D": "Animal-Related",
    "E": "Health Care", "F": "Mental Health & Crisis", "G": "Diseases & Disorders",
    "H": "Medical Research", "I": "Crime & Legal", "J": "Employment", "K": "Food, Agriculture & Nutrition",
    "L": "Housing & Shelter", "M": "Public Safety & Disaster Relief", "N": "Recreation & Sports",
    "O": "Youth Development", "P": "Human Services", "Q": "International & Foreign Affairs",
    "R": "Civil Rights & Advocacy", "S": "Community Improvement", "T": "Philanthropy & Grantmaking",
    "U": "Science & Technology", "V": "Social Science", "W": "Public & Societal Benefit",
    "X": "Religion", "Y": "Membership Benefit", "Z": "Unknown",
}

DDL = """
CREATE TABLE IF NOT EXISTS ntee (ein TEXT PRIMARY KEY, major TEXT, category TEXT);
CREATE INDEX IF NOT EXISTS idx_ntee_major ON ntee(major);
"""


def run(db=DB):
    os.makedirs(os.path.dirname(db), exist_ok=True)
    c = sqlite3.connect(db, timeout=60)
    c.executescript(DDL)
    total = 0
    for name in FILES:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=True) as tf:
            print(f"downloading {name}.csv …", flush=True)
            urllib.request.urlretrieve(BASE.format(name), tf.name)
            rows = []
            with open(tf.name, newline="", encoding="latin-1") as f:
                for r in csv.DictReader(f):
                    ein = (r.get("EIN") or "").strip()
                    code = (r.get("NTEE_CD") or "").strip()
                    if len(ein) == 9 and code and code[0] in MAJOR:
                        rows.append((ein, code[0], MAJOR[code[0]]))
            c.executemany("INSERT OR REPLACE INTO ntee (ein, major, category) VALUES (?,?,?)", rows)
            c.commit()
            total += len(rows)
            print(f"  {name}: {len(rows):,} classified orgs", flush=True)
    print(f"\nntee table: {total:,} EIN -> cause rows in {db}")
    c.close()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else DB)
