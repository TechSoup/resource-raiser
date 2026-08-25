"""Stage 2 contracts for the single-loop Agent Finder service and async client seam."""
import asyncio
import os
import sys
import threading
import unittest
from unittest import mock

import httpx
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent_finder
import ard_client
import runtime
from query_context import QueryContext
from registry import index


def hit(text, score=91):
    slug = text.lower().replace(" ", "-")
    return {"identifier": f"sources/census/{slug}.md", "title": f"Table {text}",
            "description": f"Description {text}", "queries": [text], "score": score}


class AsyncAgentFinderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        agent_finder._QUOTA.clear()

    async def _client(self, search_side_effect=None):
        app = agent_finder.create_app(llm_client=object())
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://finder.test")
        if search_side_effect is None:
            async def search_side_effect(texts, *_args, context, **_kwargs):
                return [hit(texts[0])]
        patch = mock.patch.object(index, "search_many_async", side_effect=search_side_effect)
        patch.start()
        self.addCleanup(patch.stop)
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_documented_http_contracts_keep_their_shapes(self):
        client = await self._client()
        with mock.patch.object(index, "_store", return_value=(np.zeros((2, 3)), [{}, {}])), \
             mock.patch.object(agent_finder, "_manifest", return_value={
                 "specVersion": "0.91", "entries": [], "okf:tableCount": 2}), \
             mock.patch.object(agent_finder, "_agents", return_value={
                 "entries": [], "totalSize": 2, "pageSize": 50, "pageToken": None}), \
             mock.patch.object(agent_finder, "_entry", return_value={
                 "identifier": "urn:air:test:okf:test", "data": {"mediaType": "text/markdown"}}), \
             mock.patch.object(agent_finder, "_explore", return_value={
                 "resultType": "facets", "facets": {"publisher": {"buckets": []}}}):
            root, health, manifest, agents, entry, explore, search = await asyncio.gather(
                client.get("/"), client.get("/healthz"),
                client.get("/.well-known/ard.json"), client.get("/agents"),
                client.get("/agents/entry", params={"id": "fixture"}),
                client.post("/explore", json={}),
                client.post("/search", json={"query": {"text": "Detroit"}, "pageSize": 3}))

        self.assertEqual([r.status_code for r in
                          (root, health, manifest, agents, entry, explore, search)], [200] * 7)
        self.assertEqual(set(root.json()), {"name", "store", "endpoints"})
        self.assertEqual(set(health.json()), {"ok", "entries", "dimensions"})
        self.assertTrue({"specVersion", "entries", "okf:tableCount"} <= set(manifest.json()))
        self.assertTrue({"entries", "totalSize", "pageSize", "pageToken"} <= set(agents.json()))
        self.assertTrue({"identifier", "data"} <= set(entry.json()))
        self.assertEqual(explore.json()["resultType"], "facets")
        self.assertTrue({"results", "referrals", "pageToken", "usage"} <= set(search.json()))
        self.assertEqual(search.json()["results"][0]["displayName"], "Table Detroit")

    async def test_simultaneous_searches_isolate_results_and_usage(self):
        both_started = asyncio.Event()
        started = 0

        async def fake_search(texts, *_args, context, **_kwargs):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            text = texts[0]
            context.usage_ledger.record("embed", f"model-{text}", prompt_tokens=len(text),
                                        stage=f"search-{text}")
            await asyncio.sleep(0)
            return [hit(text)]

        client = await self._client(fake_search)
        responses = await asyncio.gather(*(
            client.post("/search", json={"query": {"text": text}}) for text in ("A", "BBBB")))
        bodies = [response.json() for response in responses]
        self.assertEqual([body["results"][0]["displayName"] for body in bodies],
                         ["Table A", "Table BBBB"])
        self.assertEqual([body["usage"]["embed_tokens"] for body in bodies], [1, 4])
        self.assertEqual([set(body["usage"]["by_stage"]) for body in bodies],
                         [{"search-A"}, {"search-BBBB"}])

    async def test_relevance_failure_and_scored_refusal_keep_wire_semantics(self):
        async def relevance_failure(*_args, **_kwargs):
            raise index.RelevanceScoringError("fixture")

        client = await self._client(relevance_failure)
        failure = await client.post("/search", json={"query": {"text": "Detroit"}})
        self.assertEqual(failure.status_code, 503)
        self.assertEqual(failure.json()["code"], "relevance_scoring_failed")
        self.assertIn("usage", failure.json())

        async def no_match(*_args, **_kwargs):
            raise index.NoRelevantTablesError(top_score=49)

        with mock.patch.object(index, "search_many_async", side_effect=no_match):
            refusal = await client.post("/search", json={"query": {"text": "Detroit"}})
        self.assertEqual(refusal.status_code, 200)
        self.assertEqual(refusal.json()["results"], [])
        self.assertEqual(refusal.json()["eligibility"], {
            "status": "no_match", "threshold": 50, "topScore": 49})

    async def test_request_cancellation_cancels_pending_provider_work(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def fake_search(_texts, *_args, context, **_kwargs):
            async def provider_work():
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return await context.wait(provider_work())

        client = await self._client(fake_search)
        request = asyncio.create_task(client.post(
            "/search", json={"query": {"text": "cancel me"}}))
        await asyncio.wait_for(started.wait(), 1)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertTrue(cancelled.is_set())

    async def test_concurrency_does_not_create_request_threads(self):
        async def fake_search(texts, *_args, context, **_kwargs):
            await context.sleep(0.005)
            return [hit(texts[0])]

        client = await self._client(fake_search)
        before = threading.active_count()
        responses = await asyncio.gather(*(
            client.post("/search", json={"query": {"text": f"q{i}"}}) for i in range(40)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(threading.active_count(), before)

    async def test_event_loop_quota_update_allows_exactly_one_request(self):
        client = await self._client()
        with mock.patch.object(agent_finder, "SEARCH_LIMIT_PER_DAY", 1):
            responses = await asyncio.gather(*(
                client.post("/search", json={"query": {"text": text}}) for text in ("A", "B")))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 429])
        limited = next(response for response in responses if response.status_code == 429)
        self.assertIn("Retry-After", limited.headers)

    async def test_browser_preflight_retains_public_read_contract(self):
        client = await self._client()
        response = await client.options("/search", headers={
            "Origin": "https://example.test", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertIn("POST", response.headers["access-control-allow-methods"])
        self.assertEqual(response.headers["access-control-max-age"], "86400")

    async def test_trailing_slashes_retain_old_handler_compatibility(self):
        client = await self._client()
        with mock.patch.object(index, "_store", return_value=(np.zeros((1, 2)), [{}])):
            search, health = await asyncio.gather(
                client.post("/search/", json={"query": {"text": "Detroit"}}),
                client.get("/healthz/"))
        self.assertEqual(search.status_code, 200)
        self.assertEqual(health.status_code, 200)

    async def test_lifespan_preloads_artifacts_and_closes_owned_llm_client(self):
        owned = object()
        app = agent_finder.create_app()
        close = mock.AsyncMock()
        with mock.patch.object(index, "_store", return_value=(np.zeros((1, 2)), [{}])) as store, \
             mock.patch.object(agent_finder, "_catalog", return_value=([], {})) as catalog, \
             mock.patch.object(agent_finder.llm, "async_client", return_value=owned), \
             mock.patch.object(agent_finder.llm, "close_async_client", close):
            async with app.router.lifespan_context(app):
                self.assertIs(app.state.llm_client, owned)
        store.assert_called_once_with()
        catalog.assert_called_once_with()
        close.assert_awaited_once_with()

    async def test_async_harness_client_reuses_httpx_and_reports_finder_usage(self):
        async def fake_search(texts, *_args, context, **_kwargs):
            context.usage_ledger.record("embed", "fixture", prompt_tokens=3, stage="embed-query")
            return [hit(texts[0])]

        app_client = await self._client(fake_search)
        discovery = ard_client.DiscoveryUsage()
        context = QueryContext(http_client=app_client, discovery_ledger=discovery)
        with mock.patch.object(ard_client, "BASE", "http://finder.test"):
            first, second = await asyncio.gather(
                ard_client.search_async("Detroit", context=context),
                ard_client.search_async("Stanford", context=context))
        self.assertEqual(first[0]["title"], "Table Detroit")
        self.assertEqual(second[0]["title"], "Table Stanford")
        self.assertEqual(discovery.snapshot()["searches"], 2)
        self.assertEqual(discovery.snapshot()["embed_calls"], 2)

    async def test_async_client_requires_explicit_application_owned_http_client(self):
        with self.assertRaisesRegex(RuntimeError, "QueryContext.http_client"):
            await ard_client.search_async("Detroit", context=QueryContext())


class AsyncIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_only_async_search_preserves_union_and_source_filter(self):
        vectors = np.asarray([[1.0, 0.0], [0.0, 1.0], [.7, .7]], dtype=np.float32)
        metadata = [
            {"identifier": "sources/census/a.md", "title": "A"},
            {"identifier": "sources/census/b.md", "title": "B"},
            {"identifier": "sources/sec-edgar/c.md", "title": "C"},
        ]
        query_vectors = [[1.0, 0.0], [0.0, 1.0]]
        context = QueryContext(llm_client=object())
        with mock.patch.object(index, "_store", return_value=(vectors, metadata)), \
             mock.patch.object(index.llm, "embed_async", mock.AsyncMock(return_value=query_vectors)):
            results = await index.search_many_async(
                ["one", "two"], k=2, sources=["census"], rerank=False, context=context)
        self.assertEqual({result["identifier"] for result in results},
                         {"sources/census/a.md", "sources/census/b.md"})


if __name__ == "__main__":
    unittest.main()
