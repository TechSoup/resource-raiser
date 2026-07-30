#!/usr/bin/env python3
"""Single-source agentic data query — terminal tool.

  question  ->  Agent Finder (ARD /search) finds the right database
            ->  retrieve the data from that source (live)
            ->  answer, with provenance

Usage:
  # first, load the Azure keys:
  set -a; source ./set_keys.sh; set +a

  python3 query.py "How much did Apple spend on R&D in 2023?"
  python3 query.py            # interactive: type questions, Ctrl-D to quit
"""
import os, sys, json, subprocess
import driver
from core import Toolkit
import ard_client

ROOT = os.path.dirname(os.path.abspath(__file__))


def agent_finder(query, k=5):
    """Call the Agent Finder (ARD POST /search over HTTP)."""
    return ard_client.search(query, k)


def generic_retrieve(question, identifier, tk):
    """Skill path: read the OKF doc and let the model choose the operation+params."""
    doc = open(os.path.join(ROOT, identifier), encoding="utf-8").read()
    fm = driver.frontmatter(identifier)
    access = fm.get("access") or driver.frontmatter(
        os.path.join(os.path.dirname(identifier), fm["source"]))["access"]
    spec = json.loads(tk.llm(
        "Using the OKF source document, choose how to query it to answer the question. "
        'Respond JSON {"operation":"<name>","params":{...},"extract":"<dotted path to the value or empty>"}.',
        f"OKF DOCUMENT:\n{doc[:4000]}\n\nOPERATIONS: {json.dumps(access['operations'])}\n\nQUESTION: {question}",
        json_mode=True))
    cmd = [sys.executable, os.path.join(ROOT, "accessor", "okf_fetch.py"),
           os.path.join(ROOT, identifier), spec["operation"]]
    cmd += [f"{k}={v}" for k, v in spec.get("params", {}).items()]
    if spec.get("extract"):
        cmd += ["--extract", spec["extract"]]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(out.stderr.strip())
    return {"source": fm.get("title"), "result": out.stdout.strip()[:6000]}


def answer(question, tk):
    # 1. Agent Finder: which database(s)?
    hits = agent_finder(question, k=5)
    print("🔎 Agent Finder — candidate databases:")
    for h in hits:
        print(f"     {h['score']:5.1f}  {h['title']}")
    top = hits[0]
    fm = driver.frontmatter(top["identifier"])
    print(f"\n📚 Top database: {top['title']}")

    # 2. Retrieve from the chosen source
    print("📡 Retrieving…")
    if fm.get("concept"):                       # SEC company-financial concept
        info = json.loads(tk.llm(
            'Extract JSON {"ticker":"<US stock ticker or empty>","period":"FY<year> or latest"}.',
            question, json_mode=True))
        if not info.get("ticker"):
            print("✗ Could not identify a company in the question.")
            return
        data = driver.fetch_metric(question, info["ticker"], info.get("period", "latest"), log=True)
    else:                                        # any other single source (e.g. Treasury)
        data = generic_retrieve(question, top["identifier"], tk)
        print(f"     • {data['source']}: {data['result']}")

    # 3. Answer
    print("\n💬 " + tk.synthesize(question, data) + "\n")


def main(argv):
    import llm
    if not llm.have_credentials():
        sys.exit(llm._NO_CREDS)
    tk = Toolkit()
    if argv:
        answer(" ".join(argv), tk)
        return
    print("Agentic data query (single source). Ctrl-D to quit.")
    while True:
        try:
            q = input("\n❓ ").strip()
        except EOFError:
            break
        if q:
            try:
                answer(q, tk)
            except SystemExit as e:
                print(f"✗ {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
