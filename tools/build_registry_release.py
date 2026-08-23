#!/usr/bin/env python3
"""Release-time descriptor generation followed by immutable index publication."""
import os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATORS = ("gen_sec_okf.py", "gen_census_okf.py", "gen_treasury_okf.py",
              "gen_cdc_okf.py", "gen_np_okf.py")


def main():
    for script in GENERATORS:
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", script)], cwd=ROOT, check=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "registry", "index.py"), "build"],
                   cwd=ROOT, check=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "registry", "index.py"), "verify"],
                   cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
