#!/usr/bin/env python3
"""Unified entry point. Swap join-execution backends with --exec.

  python3 ask.py --exec planner  "What was Apple's R&D as a % of revenue in FY2023?"
  python3 ask.py --exec codegen  "What was Apple's R&D as a % of revenue in FY2023?"
"""
import sys
from core import Toolkit
from executors import EXECUTORS


def main(argv):
    ex = "planner"
    if argv and argv[0] == "--exec":
        ex, argv = argv[1], argv[2:]
    question = " ".join(argv) or "What was Apple's R&D as a percentage of revenue in FY2023?"
    if ex not in EXECUTORS:
        raise SystemExit(f"unknown executor {ex!r}; have: {list(EXECUTORS)}")
    print(f"[executor: {ex}]")
    print(EXECUTORS[ex](Toolkit()).run(question))


if __name__ == "__main__":
    main(sys.argv[1:])
