#!/usr/bin/env python3
"""Pluggable join-execution backends. Each takes the shared Toolkit and answers a
question; they differ only in HOW they combine fetched data. Add a new option by
subclassing Executor and registering it — nothing else changes.

Current backends:
  planner  — LLM decomposes into figures, fetch each, LLM computes in prose.
  codegen  — LLM writes Python that calls the toolkit and does the join/math in code.
             (executed in-process for the demo; sandbox before any real deployment.)
"""
import json, traceback


class Executor:
    name = "base"
    def __init__(self, tk):
        self.tk = tk
    def run(self, question):
        raise NotImplementedError


class PlannerExecutor(Executor):
    name = "planner"
    PLAN = ('Decompose the question into the individual figures needed. Each figure is one '
            'company + one metric + one period. Respond JSON: {"needs":[{"ticker":"AAPL",'
            '"metric":"research and development expense","period":"FY2023 or latest"}],'
            '"compute":"<what to calculate>"}. List every figure separately for ratios/comparisons.')

    def run(self, question):
        plan = json.loads(self.tk.llm(self.PLAN, question, json_mode=True))
        figures = []
        for n in plan.get("needs", []):
            try:
                figures.append(self.tk.fetch(n["metric"], n.get("ticker", ""), n.get("period", "latest")))
            except SystemExit as e:
                figures.append({"error": str(e), "need": n})
        return self.tk.synthesize(question, {"compute": plan.get("compute"), "figures": figures})


class CodegenExecutor(Executor):
    name = "codegen"
    API = (
        "Write a Python function `solve(tk)` that answers the question.\n"
        "Toolkit API (PREFER tk.lookup for most facts — it works for every domain and returns a numeric `value`; "
        "use the specific primitives below only when you need their extra fields):\n"
        "  # public companies (SEC):\n"
        "  tk.fetch(metric:str, ticker:str, period:str='latest') -> dict with keys "
        "company, metric, concept, period, value_usd, source\n"
        "  tk.resolve_entity(name:str) -> {ticker, cik, title}\n"
        "  # nonprofits — np_field/federal_funding accept an org NAME or EIN (resolution is internal):\n"
        "  tk.resolve_ein(name:str) -> {ein, name}\n"
        "  tk.np_field(field:str, org, period:str='latest') -> {organization, ein, field, period, value_usd}\n"
        "      990 fields: totrevenue, totfuncexpns, totassetsend, totcntrbgfts, compnsatncurrofcr, ...\n"
        "  tk.federal_funding(org) -> {organization, federal_awards_total_usd, award_count}\n"
        "  # universal: works for ANY domain (Census, CDC, Treasury, SEC, nonprofit, funding):\n"
        "  tk.lookup(subquestion:str) -> {source, value, data}  # `value` is the numeric answer; "
        "use tk.lookup for cross-domain joins (Census/CDC/Treasury/etc.), one fact per call\n"
        "Do all joins/arithmetic IN CODE. Return a dict "
        '{"answer": "<one concise sentence; cite the source named in each fetch dict>", "figures": [<the fetch dicts used>]}. '
        "You may use json/math. Return ONLY python, no markdown fences."
    )

    def _gen(self, question, err):
        u = f"Question: {question}"
        if err:
            u += f"\n\nYour previous code raised:\n{err}\nReturn a corrected `solve`."
        raw = self.tk.llm(self.API, u)
        return raw.replace("```python", "").replace("```", "").strip()

    def run(self, question, retries=2):
        code = self._gen(question, None)
        for _ in range(retries + 1):
            try:
                ns = {}
                exec(code, ns)
                res = ns["solve"](self.tk)
                return res["answer"] if isinstance(res, dict) and "answer" in res else str(res)
            except Exception:
                code = self._gen(question, traceback.format_exc()[-1500:])
        return "codegen failed after retries"


EXECUTORS = {e.name: e for e in (PlannerExecutor, CodegenExecutor)}
