#!/usr/bin/env python3
"""Stream the IRS 990 e-file ZIPs for 2022-2024 into the grant-edge table, one ZIP at a time.

Disk-frugal: download one monthly ZIP, unzip to a temp dir, extract edges, delete both. Peak
disk is ~one ZIP (a few hundred MB), never the whole ~12 GB corpus. Resumable — a ZIP already
recorded in the done_files table is skipped, so a re-run continues where it stopped.
"""
import os, sys, urllib.request, tempfile
import grants_etl as G

BASE = "https://apps.irs.gov/pub/epostcard/990/xml/{y}/{y}_TEOS_XML_{mm}A.zip"
YEARS = [2022, 2023, 2024]


def _exists(url):
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length", 0)) > 1000
    except Exception:
        return False


def _download(url, dest):
    urllib.request.urlretrieve(url, dest)
    return os.path.getsize(dest)


def run(db=G.DB):
    urls = [BASE.format(y=y, mm=f"{m:02d}") for y in YEARS for m in range(1, 13)]
    urls = [u for u in urls if _exists(u)]
    print(f"{len(urls)} monthly ZIPs to process for {YEARS}\n", flush=True)
    grand = 0
    for i, url in enumerate(urls, 1):
        tag = os.path.basename(url).replace(".zip", "")
        c = G._conn(db)
        seen = c.execute("SELECT edges FROM done_files WHERE name=?", (tag,)).fetchone()
        c.close()
        if seen:
            print(f"[{i}/{len(urls)}] {tag}: already done ({seen[0]:,} edges)", flush=True)
            grand += seen[0]
            continue
        with tempfile.TemporaryDirectory(prefix="g990dl_") as td:
            z = os.path.join(td, tag + ".zip")
            try:
                mb = _download(url, z) / 1e6
                print(f"[{i}/{len(urls)}] {tag}: downloaded {mb:.0f} MB, parsing…", flush=True)
                n = G.parse_zip(z, db)   # unzips to its own temp dir, parses, cleans up
                grand += n
            except Exception as e:
                print(f"[{i}/{len(urls)}] {tag}: ERROR {str(e)[:120]}", flush=True)
    print(f"\nDONE — {grand:,} total edges in {db}", flush=True)
    G.stats(db)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else G.DB)
