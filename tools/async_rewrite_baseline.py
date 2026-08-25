#!/usr/bin/env python3
"""Record the sync implementation's semantic, latency, LLM, and thread baseline.

Examples:
  .venv/bin/python tools/async_rewrite_baseline.py homepage --url http://127.0.0.1:8099/ask \
      --harness-pid 123 --finder-pid 124 --output tests/baselines/homepage-7da2074.json
  .venv/bin/python tools/async_rewrite_baseline.py synthetic-delay \
      --output tests/baselines/synthetic-delay-7da2074.json
"""
import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _thread_count(pid):
    if not pid:
        return None
    proc_tasks = f"/proc/{pid}/task"
    if os.path.isdir(proc_tasks):
        return len(os.listdir(proc_tasks))
    try:
        lines = subprocess.check_output(
            ["ps", "-M", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL).splitlines()
        return max(0, len(lines) - 1)
    except (OSError, subprocess.SubprocessError):
        return None


class ThreadSampler:
    def __init__(self, harness_pid=None, finder_pid=None):
        self.pids = {"harness": harness_pid, "finder": finder_pid}
        self.before = {name: _thread_count(pid) for name, pid in self.pids.items()}
        self.peaks = dict(self.before)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, name="baseline-thread-sampler", daemon=True)

    def _sample(self):
        while not self.stop.wait(0.01):
            for name, pid in self.pids.items():
                count = _thread_count(pid)
                if count is not None:
                    self.peaks[name] = max(self.peaks.get(name) or 0, count)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=1)

    def snapshot(self):
        return {name: {"pid": pid, "before": self.before[name], "peak": self.peaks[name],
                       "after": _thread_count(pid)}
                for name, pid in self.pids.items()}


def _answer_value(content):
    evidence = content.get("evidence") or {}
    if evidence.get("value") is not None:
        return evidence["value"]
    data = content.get("data") or {}
    for key in ("value", "value_usd", "total_usd", "status", "matches", "count"):
        if data.get(key) is not None:
            return data[key]
    return None


def ask(url, question, timeout):
    started = time.monotonic()
    request = urllib.request.Request(
        url, data=json.dumps({"query": question, "streaming": False}).encode(),
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
        content = next((m.get("content") for m in reversed(body.get("messages") or [])
                        if m.get("message_type") == "nlws"), None)
        error = next((m.get("content") for m in reversed(body.get("messages") or [])
                      if m.get("message_type") == "error"), None)
        content = content if isinstance(content, dict) else {}
        usage, discovery = content.get("usage") or {}, content.get("discovery_usage") or {}
        evidence = content.get("evidence") or {}
        return {
            "question": question,
            "status": content.get("status") or ("error" if error else "no_result"),
            "source_identifier": evidence.get("identifier"),
            "answer_value": _answer_value(content),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "llm_calls": (usage.get("llm_calls") or 0) + (discovery.get("llm_calls") or 0),
            "error": error,
        }
    except Exception as exc:
        return {"question": question, "status": "transport_error", "source_identifier": None,
                "answer_value": None, "latency_ms": round((time.monotonic() - started) * 1000),
                "llm_calls": 0, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _run_batch(url, questions, workers, timeout, harness_pid=None, finder_pid=None):
    started = time.monotonic()
    with ThreadSampler(harness_pid, finder_pid) as sampler, \
         ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ask, url, question, timeout): i
                   for i, question in enumerate(questions)}
        indexed = {}
        for future in as_completed(futures):
            indexed[futures[future]] = future.result()
    return {
        "workers": workers,
        "wall_ms": round((time.monotonic() - started) * 1000),
        "thread_counts": sampler.snapshot(),
        "results": [indexed[i] for i in range(len(questions))],
    }


def homepage(args):
    import harness
    questions = [entry["q"] if isinstance(entry, dict) else entry
                 for tab in harness.EXAMPLE_TABS for entry in tab["queries"]]
    if len(questions) != 66:
        raise SystemExit(f"homepage contract changed: expected 66 entries, found {len(questions)}")
    return {"kind": "homepage", "url": args.url, "query_count": len(questions),
            "run": _run_batch(args.url, questions, args.workers, args.timeout,
                              args.harness_pid, args.finder_pid)}


def synthetic_delay(args):
    import ard_client
    import harness
    servers = queue.Queue()

    def delayed(question, **_):
        time.sleep(args.delay_ms / 1000)
        return {"question": question, "status": "answered", "answer": "fixture",
                "shape": "point", "plan": "synthetic delay", "usage": {"llm_calls": 0},
                "discovery_usage": {"llm_calls": 0}, "source": {}, "candidates": [],
                "data": {"value": 1}, "evidence": {"value": 1}, "attempts": []}

    with tempfile.TemporaryDirectory() as td, \
         mock.patch.object(harness, "run", side_effect=delayed), \
         mock.patch.object(harness, "_quota_check", return_value=(True, 0, 0)), \
         mock.patch.object(harness, "TELEMETRY_PATH", os.path.join(td, "telemetry.jsonl")), \
         mock.patch.object(harness, "OPERATIONAL_TELEMETRY_PATH",
                           os.path.join(td, "operations.jsonl")), \
         mock.patch.object(harness, "TELEMETRY_STDOUT", False), \
         mock.patch.object(ard_client, "health", return_value={"ok": True, "entries": 1}):
        thread = threading.Thread(target=harness.serve, args=(0, servers.put), daemon=True)
        thread.start()
        server = servers.get(timeout=3)
        url = f"http://127.0.0.1:{server.server_address[1]}/ask"
        try:
            runs = []
            for concurrency in (1, 4, 8):
                questions = [f"synthetic delay {concurrency}-{i}" for i in range(concurrency)]
                runs.append(_run_batch(url, questions, concurrency, args.timeout,
                                       os.getpid(), None))
        finally:
            server.shutdown()
            thread.join(timeout=3)
    return {"kind": "synthetic-delay", "delay_ms": args.delay_ms, "runs": runs}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("homepage", "synthetic-delay"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--url", default=os.getenv("ARD_URL", "http://127.0.0.1:8099/ask"))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--harness-pid", type=int)
    parser.add_argument("--finder-pid", type=int)
    parser.add_argument("--delay-ms", type=int, default=200)
    args = parser.parse_args()

    payload = homepage(args) if args.mode == "homepage" else synthetic_delay(args)
    payload.update({
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": _git("rev-parse", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
        "command": shlex.join([sys.executable, *sys.argv]),
    })
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(args.output)


if __name__ == "__main__":
    main()
