#!/usr/bin/env python3
"""Multi-source / multi-dataset join queries.

  question
    -> LLM planner: decompose into the individual figures needed
    -> for each: ARD discovery -> resolve -> accessor (reuses driver.fetch_metric)
    -> LLM: compute (ratios, comparisons, growth) + synthesize a cited answer

Each figure is independently discovered and fetched from SEC EDGAR (a separate
OKF dataset entry), then joined. Run after sourcing the Azure keys:
    python3 driver_join.py "What was Apple's R&D as a percent of revenue in 2023?"
"""
import sys, json
from driver import ask_llm, fetch_metric

PLAN = (
    "Decompose the question into the individual financial figures needed to answer it. "
    "Each figure is one company + one metric + one period. "
    'Respond JSON: {"needs": [{"ticker": "AAPL", "metric": "research and development expense", '
    '"period": "FY2023 or latest"}], "compute": "<what to calculate or compare>"}. '
    "For ratios, comparisons, or growth, list every figure as a separate need."
)
SYNTH = (
    "Answer the user's question using ONLY the provided data. Show the arithmetic "
    "(ratios, differences, % change) explicitly with the numbers used. Cite SEC EDGAR. Be concise."
)


def main(question):
    plan = json.loads(ask_llm(PLAN, question, json_mode=True))
    needs = plan.get("needs", [])
    print(f"• plan: {len(needs)} figures — {plan.get('compute','')}")
    results = []
    for n in needs:
        try:
            results.append(fetch_metric(n["metric"], n.get("ticker", ""), n.get("period", "latest")))
        except SystemExit as e:
            print(f"  ! {e}")
    if not results:
        raise SystemExit("no data fetched")
    print("\n" + ask_llm(SYNTH, json.dumps({"question": question, "compute": plan.get("compute"), "data": results})))


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "What was Apple's R&D as a percent of revenue in FY2023?")
