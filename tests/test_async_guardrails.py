"""Behavioral contracts that the staged asyncio rewrite must preserve."""
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ARD_STORE", "json")

import ard_client
import connectors
import harness
import nlweb
import runtime
from domain import Evidence


with open(os.path.join(ROOT, "tests", "fixtures", "async_http_contracts.json")) as f:
    CONTRACTS = json.load(f)["cases"]


def _engine_result(case):
    """Build the smallest real engine result that exercises the NLWeb HTTP boundary."""
    evidence = {
        "kind": "status" if case["shape"] == "status" else "point",
        "source": case["publisher"],
        "identifier": case["source"],
        "entity": {"label": case["entity"],
                   **({"qid": case["entity_qid"]} if case.get("entity_qid") else {}),
                   **({"ein": case["ein"]} if case.get("ein") else {})},
        "value": case["value"],
        "unit": case["unit"],
    }
    return {
        "question": case["question"], "status": case["status"],
        "answer": f"fixture answer: {case['value']}", "shape": case["shape"],
        "plan": "stage-0 contract fixture", "usage": {"llm_calls": 2},
        "discovery_usage": {"llm_calls": 2}, "intent": {}, "attempts": [],
        "evidence": evidence, "answer_renderer": evidence["kind"],
        "source": {"identifier": case["source"], "title": case["publisher"],
                   "publisher": case["publisher"]},
        "candidates": [], "data": {"value": case["value"], "unit": case["unit"],
                                     "entity": evidence["entity"]},
    }


class HttpContractFixtureTests(unittest.TestCase):
    """Named regressions cross the same POST /ask parser and NLWeb serializer as production."""

    def test_named_point_and_clarification_follow_up_contracts(self):
        seen = []

        def fake_run(question, sites=None, assumptions=None, on_ambiguity="answer"):
            assumptions = assumptions or {}
            matches = [c for c in CONTRACTS if c["question"] == question and
                       c.get("assumptions", {}) == assumptions]
            self.assertEqual(len(matches), 1, (question, assumptions))
            seen.append((matches[0]["id"], assumptions))
            return _engine_result(matches[0])

        servers = queue.Queue()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(harness, "TELEMETRY_PATH", os.path.join(td, "telemetry.jsonl")), \
             mock.patch.object(harness, "OPERATIONAL_TELEMETRY_PATH",
                               os.path.join(td, "operations.jsonl")), \
             mock.patch.object(harness, "TELEMETRY_STDOUT", False), \
             mock.patch.object(harness, "_quota_check", return_value=(True, 0, 0)), \
             mock.patch.object(ard_client, "health", return_value={"ok": True, "entries": 10425}), \
             mock.patch.object(harness, "run", side_effect=fake_run):
            thread = threading.Thread(target=harness.serve, args=(0, servers.put), daemon=True)
            thread.start()
            server = servers.get(timeout=3)
            url = f"http://127.0.0.1:{server.server_address[1]}/ask"
            try:
                for case in CONTRACTS:
                    request = urllib.request.Request(
                        url,
                        data=json.dumps({"query": case["question"], "streaming": False,
                                         "assumptions": case.get("assumptions", {})}).encode(),
                        headers={"content-type": "application/json"})
                    with self.subTest(case=case["id"]), \
                         urllib.request.urlopen(request, timeout=5) as response:
                        body = json.load(response)
                    content = next(m["content"] for m in body["messages"]
                                   if m["message_type"] == nlweb.NLWS)
                    self.assertEqual(content["status"], case["status"])
                    self.assertEqual(content["shape"], case["shape"])
                    self.assertEqual(content["evidence"]["identifier"], case["source"])
                    self.assertEqual(content["evidence"]["value"], case["value"])
                    self.assertEqual(content["evidence"]["entity"]["label"], case["entity"])
                    if case.get("entity_qid"):
                        self.assertEqual(content["evidence"]["entity"]["qid"], case["entity_qid"])
                    if case.get("ein"):
                        self.assertEqual(content["evidence"]["entity"]["ein"], case["ein"])
            finally:
                server.shutdown()
                thread.join(timeout=3)

        self.assertEqual([case_id for case_id, _ in seen], [c["id"] for c in CONTRACTS])
        self.assertEqual(seen[-2][1]["entity_qid"], "Q508775")
        self.assertEqual(seen[-1][1]["entity_qid"], "Q17061198")

    def test_named_sse_order_crosses_the_http_boundary(self):
        servers = queue.Queue()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(harness, "TELEMETRY_PATH", os.path.join(td, "telemetry.jsonl")), \
             mock.patch.object(harness, "OPERATIONAL_TELEMETRY_PATH",
                               os.path.join(td, "operations.jsonl")), \
             mock.patch.object(harness, "TELEMETRY_STDOUT", False), \
             mock.patch.object(harness, "_quota_check", return_value=(True, 0, 0)), \
             mock.patch.object(harness, "run", return_value=StreamLifecycleTests._result()):
            thread = threading.Thread(target=harness.serve, args=(0, servers.put), daemon=True)
            thread.start()
            server = servers.get(timeout=3)
            url = (f"http://127.0.0.1:{server.server_address[1]}/ask"
                   "?query=q&sse_format=named")
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/event-stream")
                    events = []
                    while True:
                        line = response.readline().decode()
                        if not line:
                            break
                        if line.startswith("event: "):
                            events.append(line.removeprefix("event: ").strip())
                            if events[-1] == nlweb.END:
                                break
            finally:
                server.shutdown()
                thread.join(timeout=3)

        self.assertEqual(events, [nlweb.BEGIN, nlweb.NLWS, nlweb.COMPLETE, nlweb.END])


class StreamLifecycleTests(unittest.TestCase):
    REQUEST = {"query": "q", "sites": (), "conversation_id": None, "min_score": 0,
               "max_results": 10, "mode": "generate", "debug": False}

    @staticmethod
    def _result(answer="done"):
        return {"answer": answer, "candidates": [], "source": {}, "data": {"value": answer},
                "evidence": {"value": answer}, "usage": {}, "discovery_usage": {},
                "shape": "point", "plan": "fixture"}

    def test_named_sse_events_preserve_protocol_order_and_sequence(self):
        with mock.patch.object(harness, "run", return_value=self._result()):
            messages = list(harness.run_nlweb(dict(self.REQUEST)))
        frames = b"".join(nlweb.encode(message, named=True) for message in messages).decode()
        named = [line.removeprefix("event: ") for line in frames.splitlines()
                 if line.startswith("event: ")]
        self.assertEqual(named, [nlweb.BEGIN, nlweb.NLWS, nlweb.COMPLETE, nlweb.END])
        self.assertEqual([m["sequence"] for m in messages], list(range(1, len(messages) + 1)))

    def test_simultaneous_responses_do_not_cross_progress_or_answers(self):
        rendezvous = threading.Barrier(2)

        def fake_run(question, **_):
            harness._say("status", icon="x", msg=f"progress-{question}")
            rendezvous.wait(timeout=2)
            return self._result(f"answer-{question}")

        def consume(question):
            return list(harness.run_nlweb({**self.REQUEST, "query": question}))

        with mock.patch.object(harness, "run", side_effect=fake_run), \
             ThreadPoolExecutor(max_workers=2) as pool:
            streams = list(pool.map(consume, ("A", "B")))

        for question, messages in zip(("A", "B"), streams):
            progress = [m["content"] for m in messages if m["message_type"] == nlweb.INTERMEDIATE]
            answer = next(m["content"]["answer"] for m in messages
                          if m["message_type"] == nlweb.NLWS)
            self.assertEqual(progress, [f"x progress-{question}"])
            self.assertEqual(answer, f"answer-{question}")

    def test_deadline_is_a_terminal_protocol_error(self):
        def slow_run(*_, **__):
            time.sleep(0.05)
            runtime.check()

        with mock.patch.dict(os.environ, {"QUERY_TIMEOUT_SECONDS": "0"}), \
             mock.patch.object(harness, "run", side_effect=slow_run):
            messages = list(harness.run_nlweb(dict(self.REQUEST)))
        self.assertEqual([m["message_type"] for m in messages],
                         [nlweb.BEGIN, nlweb.ERROR, nlweb.END])
        self.assertIn("deadline exceeded", messages[1]["content"])

    def test_closing_stream_signals_cancellation_to_worker(self):
        cancelled = threading.Event()

        def cancellable_run(*_, **__):
            harness._say("status", icon="x", msg="started")
            while True:
                try:
                    runtime.check()
                except runtime.QueryCancelled:
                    cancelled.set()
                    return self._result("cancelled")
                time.sleep(0.005)

        with mock.patch.object(harness, "run", side_effect=cancellable_run):
            stream = harness.run_nlweb(dict(self.REQUEST))
            self.assertEqual(next(stream)["message_type"], nlweb.BEGIN)
            self.assertEqual(next(stream)["message_type"], nlweb.INTERMEDIATE)
            stream.close()
            self.assertTrue(cancelled.wait(1), "closing a client stream did not cancel its worker")


class ConcurrentSearchGuardrailTests(unittest.TestCase):
    def test_prune_and_attempt_budgets_are_isolated_between_fanout_branches(self):
        branch_b_started = threading.Event()
        bad_pruned = threading.Event()

        class FixtureConnector:
            def execute(self, intent, attempt, hit, executor, adjudicator=None):
                if intent.question == "branch B":
                    branch_b_started.set()
                    if not bad_pruned.wait(2):
                        raise AssertionError("branch A never pruned its rejected table")
                elif hit["identifier"] == "bad-A":
                    if not branch_b_started.wait(2):
                        raise AssertionError("branch B was not in flight before the prune")
                    bad_pruned.set()
                    raise connectors.Rejected("wrong table", attempt)
                return Evidence(kind="point", source=hit["title"],
                                identifier=hit["identifier"], payload={"value": 1}, value=1)

        contexts = {name: {"shape": "point", "entity": "", "entity_status": "none",
                           "attribute": "value", "period": "FY2020"}
                    for name in ("branch A", "branch B")}
        hits = {
            "branch A": [{"identifier": "bad-A", "title": "bad A", "publisher": "fixture"},
                         {"identifier": "good-A", "title": "good A", "publisher": "fixture"}],
            "branch B": [{"identifier": "good-B", "title": "good B", "publisher": "fixture"}],
        }

        def search(question):
            return harness._search(question, contexts[question], hits[question])

        connector = FixtureConnector()
        with mock.patch.object(harness, "MAX_SEARCH_ATTEMPTS", 2), \
             mock.patch.object(harness, "_link_entity", return_value=[None]), \
             mock.patch.object(harness, "_key_options", return_value=["k1", "k2", "k3"]), \
             mock.patch.object(connectors, "for_hit", return_value=connector), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(search, ("branch A", "branch B")))

        self.assertEqual(results[0][2]["identifier"], "good-A")
        self.assertEqual(results[1][2]["identifier"], "good-B")
        self.assertEqual([a.identifier for a in results[0][5]["_attempts"]], ["bad-A", "good-A"])
        self.assertEqual([a.identifier for a in results[1][5]["_attempts"]], ["good-B"])


if __name__ == "__main__":
    unittest.main()
