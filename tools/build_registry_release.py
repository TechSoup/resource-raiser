#!/usr/bin/env python3
"""Release-time descriptor generation followed by immutable index publication."""
import os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from registry import index

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    for script in index.RELEASE_GENERATORS:
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", script)], cwd=ROOT, check=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "registry", "index.py"), "build", "--release"],
                   cwd=ROOT, check=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "registry", "index.py"), "verify", "--release"],
                   cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
