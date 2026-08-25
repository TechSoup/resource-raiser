"""Stage 5: sequential async point engine, named regressions, and cancellation."""
import asyncio
import json
import os
import sys
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ard_client
import connectors
import college
import driver
import fema
from domain import Evidence
import harness
import llm
import nonprofit
import orgprofile
from query_context import QueryContext
import resolver
import runtime


with open(os.path.join(ROOT, "tests", "fixtures", "async_http_contracts.json")) as stream:
    NAMED_CASES = json.load(stream)["cases"]


def context():
    return QueryContext(usage_ledger=llm.Ledger(), discovery_ledger=ard_client.DiscoveryUsage())


class AsyncPointEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_uses_shared_classifier_prompt_and_full_question_rerank(self):
        ctx = context()
        classification = {"entity": "Detroit", "canonical_entity": "Detroit, Michigan",
                          "entity_status": "resolved", "entity_candidates": [], "entities": [],
                          "type": "place", "attribute": "broadband rate", "period": "latest",
                          "periods": [], "sources": ["census"], "shape": "point",
                          "threshold": None, "quantifier": "exhaustive", "interpretations": []}
        hit = {"identifier": "sources/census/dp02-0154pe.md", "title": "Broadband",
               "publisher": "census", "score": 100}
        with mock.patch.object(llm, "chat_async",
                               mock.AsyncMock(return_value=json.dumps(classification))) as chat, \
             mock.patch.object(ard_client, "search_many_async",
                               mock.AsyncMock(return_value=[hit])) as search, \
             mock.patch.object(ard_client, "search_many",
                               side_effect=AssertionError("sync Finder called")):
            actual, hits = await harness.discover_async(
                "What is broadband access in Detroit?", assumptions="not-a-dict", context=ctx)
        self.assertEqual(actual["canonical_entity"], "Detroit, Michigan")
        self.assertEqual(hits, [hit])
        self.assertEqual(chat.await_args.args[0], harness._discovery_system(
            "\n".join(f"- {directory}: covers {entity_type}"
                      for directory, entity_type in harness.SOURCE_TYPES.items())))
        self.assertEqual(search.await_args.kwargs["rerank_query"],
                         "What is broadband access in Detroit?")

    async def test_entity_linking_judges_candidates_with_full_question(self):
        ctx = context()
        candidates = [{"id": f"Q{index}", "label": f"Stanford {index}",
                       "description": "fixture"} for index in range(7)]
        with mock.patch.object(resolver, "search_async",
                               mock.AsyncMock(return_value=candidates)) as search, \
             mock.patch.object(llm, "chat_async",
                               mock.AsyncMock(return_value='{"indices":[3]}')) as chat, \
             mock.patch.object(resolver, "claims_async",
                               mock.AsyncMock(return_value=("Stanford University", {"ein": "1"}))):
            result = await harness._link_records_async(
                "Stanford", "How much NIH funding does Stanford get?", "org", context=ctx)
        self.assertEqual(result[0]["label"], "Stanford University")
        self.assertNotIn("limit", search.await_args.kwargs)
        listing = "\n".join(
            f"{index}. {candidate['label']} — {candidate['description']}"
            for index, candidate in enumerate(candidates))
        self.assertEqual(chat.await_args.args[0], harness._entity_selection_system(
            "Stanford", "org", "How much NIH funding does Stanford get?", listing))

    async def test_named_point_contracts_all_pass_through_run_concurrently(self):
        by_question = {}
        for case in NAMED_CASES:
            by_question.setdefault(case["question"], []).append(case)

        async def discover(question, sites=None, assumptions=None, context=None):
            choices = by_question[question]
            case = next((item for item in choices if item.get("assumptions") == assumptions), choices[0])
            shape = case["shape"]
            ctx = {"question": question, "entity": case["entity"], "canonical_entity": case["entity"],
                   "entity_status": "resolved", "entity_candidates": [],
                   "entity_qid": case.get("entity_qid", ""),
                   "type": "place" if case["id"].startswith("detroit") else
                           ("company" if case["id"].startswith("microsoft") else "nonprofit"),
                   "attribute": "501(c)(3) status" if shape == "status" else "requested value",
                   "shape": shape, "period": "latest", "sources": [case["publisher"]],
                   "entities": [], "interpretations": [], "threshold": None,
                   "quantifier": "exhaustive", "_case": case}
            hit = {"identifier": case["source"], "title": case["publisher"],
                   "publisher": case["publisher"], "score": 100}
            return ctx, [hit]

        async def link(ctx, context=None):
            case = ctx["_case"]
            return [{"qid": case.get("entity_qid", "Q1"), "label": case["entity"], "keys": {}}]

        async def keys(state, ctx, context=None):
            return ["fixture-key"]

        async def fetch(state, ctx, context=None):
            case = ctx["_case"]
            if case["shape"] == "status":
                return {"organization": case["entity"], "is_501c3": bool(case["value"]),
                        "value": bool(case["value"]), "source": case["publisher"]}
            if case["shape"] == "entity-list":
                return {"organization": case["entity"], "record_count": 2,
                        "total_usd": case["value"],
                        "total_usd_display": "${:,.0f}".format(case["value"]),
                        "complete": True, "results": [], "source": case["publisher"]}
            return {"entity": {"label": case["entity"]}, "metric": "requested value",
                    "value": case["value"], "unit": case.get("unit"),
                    "source": case["publisher"]}

        with mock.patch.object(harness, "discover_async", side_effect=discover), \
             mock.patch.object(harness, "_link_entity_async", side_effect=link), \
             mock.patch.object(harness, "_key_options_async", side_effect=keys), \
             mock.patch.object(harness, "_fetch_async", side_effect=fetch), \
             mock.patch.object(harness, "_answers_async", mock.AsyncMock(return_value=(True, ""))), \
             mock.patch.object(harness.TK, "synthesize_async",
                               mock.AsyncMock(return_value="fixture answer")), \
             mock.patch.object(ard_client, "search_many",
                               side_effect=AssertionError("sync Finder called")), \
             mock.patch.object(driver, "accessor", side_effect=AssertionError("sync fetch called")), \
             mock.patch.object(connectors.Connector, "execute",
                               side_effect=AssertionError("sync connector called")):
            inputs = [(case, context()) for case in NAMED_CASES]
            results = await asyncio.gather(*(
                harness.run(case["question"], assumptions=case.get("assumptions"), context=ctx)
                for case, ctx in inputs))

        self.assertEqual(len(results), len(NAMED_CASES))
        for (case, ctx), result in zip(inputs, results):
            self.assertEqual(result["source"]["identifier"], case["source"])
            self.assertEqual(result["evidence"]["value"], case["value"])
            self.assertEqual(result["shape"], case["shape"])
            self.assertEqual(len(ctx.memo["attempts"]), 1)
            self.assertEqual(ctx.memo["attempts"][0].outcome, "accepted")

    async def test_every_point_strategy_dispatches_to_an_async_implementation(self):
        cases = [
            ({"concept": "X"}, "concept"),
            ({"classification": True}, "classification"),
            ({"field": "totrevenue"}, "field"),
            ({"bmf": "eligibility"}, "bmf"),
            ({"profile": "overview"}, "profile"),
            ({"scorecard": "tuition"}, "scorecard"),
            ({"fema": True}, "fema"),
            ({"variable": "X"}, "rest"),
            ({"search": {"want": "organization"}}, "search"),
        ]
        state = {"hit": {"identifier": "fixture", "title": "fixture"},
                 "entity": {"qid": "Q1", "label": "Fixture", "keys": {}},
                 "key": "fixture-key", "period": "latest"}
        query = {"attribute": "value", "entity": "Fixture"}
        with mock.patch.object(harness, "_s_concept_async",
                               mock.AsyncMock(return_value={"tag": "concept"})), \
             mock.patch.object(nonprofit, "classify_async",
                               mock.AsyncMock(return_value={"tag": "classification"})), \
             mock.patch.object(nonprofit, "fetch_np_async",
                               mock.AsyncMock(return_value={"tag": "field"})), \
             mock.patch.object(nonprofit, "bmf_async", mock.AsyncMock(return_value={"tag": "bmf"})), \
             mock.patch.object(orgprofile, "fetch_async",
                               mock.AsyncMock(return_value={"tag": "profile"})), \
             mock.patch.object(college, "fetch_async",
                               mock.AsyncMock(return_value={"tag": "scorecard"})), \
             mock.patch.object(fema, "fetch_async", mock.AsyncMock(return_value={"tag": "fema"})), \
             mock.patch.object(harness, "_s_rest_async", mock.AsyncMock(return_value={"tag": "rest"})), \
             mock.patch.object(harness, "_s_search_async",
                               mock.AsyncMock(return_value={"tag": "search"})):
            for frontmatter, expected in cases:
                with self.subTest(expected):
                    with mock.patch.object(driver, "frontmatter", return_value=frontmatter):
                        result = await harness._fetch_async(state, query, context=context())
                    self.assertEqual(result["tag"], expected)

    async def test_async_solver_preserves_depth_first_prune_order(self):
        visited = []
        async def goal(state):
            visited.append((state["hit"], state["key"]))
            if state["hit"] == "bad":
                raise harness.Prune("hit", "wrong table")
            if state["key"] == 1:
                raise harness.Backtrack("no data")
            return state
        result = await harness._solve_async(
            [("hit", lambda state: ["bad", "good"]), ("key", lambda state: [1, 2])], goal, {})
        self.assertEqual(visited, [("bad", 1), ("good", 1), ("good", 2)])
        self.assertEqual(result, {"hit": "good", "key": 2})

    async def test_search_connector_rejection_prunes_the_real_key_subtree(self):
        ctx = context()
        query_context = {"question": "fixture", "entity": "", "canonical_entity": "",
                         "entity_status": "none", "entity_candidates": [], "type": "none",
                         "attribute": "value", "shape": "point", "period": "latest",
                         "entities": [], "interpretations": []}
        hits = [{"identifier": "bad", "title": "bad", "publisher": "fixture", "score": 100},
                {"identifier": "good", "title": "good", "publisher": "fixture", "score": 90}]
        visited = []
        class Connector:
            async def execute_async(self, intent, attempt, hit, executor, adjudicator=None):
                visited.append((hit["identifier"], attempt.request.get("key")))
                if hit["identifier"] == "bad":
                    attempt.outcome, attempt.reason = "rejected", "wrong table"
                    raise connectors.Rejected("wrong table", attempt)
                return Evidence(kind="point", source="good", identifier="good",
                                payload={"value": 1}, value=1)
        async def keys(state, ctx, context=None):
            # Put the chosen key on the attempt where the probe can observe it.
            return ["k1", "k2", "k3"]
        async def fetch(state, query, context=None):
            return {"value": 1, "key": state["key"]}
        connector = Connector()
        # Capture the selected key without weakening the real connector-rejection path.
        async def execute(intent, attempt, hit, executor, adjudicator=None):
            data = await executor()
            attempt.request["key"] = data["key"]
            return await connector.execute_async(intent, attempt, hit, lambda: data, adjudicator)
        proxy = mock.Mock(execute_async=execute)
        with mock.patch.object(harness, "_link_entity_async", mock.AsyncMock(return_value=[None])), \
             mock.patch.object(harness, "_key_options_async", side_effect=keys), \
             mock.patch.object(harness, "_fetch_async", side_effect=fetch), \
             mock.patch.object(connectors, "for_hit", return_value=proxy):
            result = await harness._search_async("fixture", query_context, hits, context=ctx)
        self.assertEqual(visited, [("bad", "k1"), ("good", "k1")])
        self.assertEqual(result[2]["identifier"], "good")
        self.assertEqual([attempt.identifier for attempt in result[5]["_attempts"]], ["bad", "good"])

    async def test_cancellation_during_fetch_records_cancelled_attempt(self):
        ctx = context()
        question_context = {"question": "cancel me", "entity": "", "canonical_entity": "",
                            "entity_status": "none", "entity_candidates": [], "type": "none",
                            "attribute": "value", "shape": "point", "period": "latest",
                            "entities": [], "interpretations": []}
        hit = {"identifier": "sources/treasury/debt-to-penny-tot-pub-debt-out-amt.md",
               "title": "fixture", "publisher": "treasury", "score": 100}
        started = asyncio.Event()
        async def fetch(*args, **kwargs):
            started.set()
            await ctx.wait(asyncio.Event().wait())
        with mock.patch.object(harness, "_link_entity_async", mock.AsyncMock(return_value=[None])), \
             mock.patch.object(harness, "_key_options_async", mock.AsyncMock(return_value=[None])), \
             mock.patch.object(harness, "_fetch_async", side_effect=fetch):
            task = asyncio.create_task(
                harness._search_async("cancel me", question_context, [hit], context=ctx))
            await started.wait()
            ctx.cancel()
            with self.assertRaises(runtime.QueryCancelled):
                await task
        self.assertEqual(len(ctx.memo["attempts"]), 1)
        self.assertEqual(ctx.memo["attempts"][0].outcome, "error")
        self.assertIn("cancel", ctx.memo["attempts"][0].reason)

    async def test_point_concurrency_creates_no_worker_threads(self):
        before = {thread.ident for thread in threading.enumerate()}
        async def goal(state):
            await asyncio.sleep(0)
            return state
        await asyncio.gather(*(harness._solve_async(
            [("hit", lambda state: [index])], goal, {}) for index in range(20)))
        after = {thread.ident for thread in threading.enumerate()}
        self.assertEqual(after, before)


class AsyncConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_connector_awaits_executor_and_adjudicator(self):
        intent = harness.QueryIntent("q", operation="point", measure="value")
        attempt = harness.Attempt("fixture", "fixture")
        hit = {"identifier": "fixture", "title": "fixture"}
        async def execute(): return {"value": 3, "source": "fixture"}
        async def adjudicate(data, verdict): return True, ""
        with mock.patch.object(connectors.GENERIC, "validate") as validate:
            validate.return_value = mock.Mock(accepted=True, residual_semantic_check=True)
            evidence = await connectors.GENERIC.execute_async(
                intent, attempt, hit, execute, adjudicator=adjudicate)
        self.assertEqual(evidence.value, 3)
        self.assertEqual(attempt.outcome, "accepted")


if __name__ == "__main__":
    unittest.main()
