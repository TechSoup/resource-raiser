"""Stages 3–4 contracts for thread-free publisher, entity, BigQuery, and Postgres I/O."""
import asyncio
import decimal
import json
import inspect
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from accessor import okf_fetch
import bq
import driver
import grants
import resolver
import runtime
import source_clients
from query_context import QueryContext


def descriptor(path, url, method="GET", body=None):
    operation = {"method": method, "url": url}
    if body is not None:
        operation["body"] = body
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("---\n" + json.dumps({"access": {"operations": {"get": operation}}}) + "\n---\n")


class AsyncAccessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_retries_transient_status_without_blocking_sleep(self):
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(503 if calls == 1 else 200, json={"ok": True})
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                path = os.path.join(directory, "source.md")
                descriptor(path, "https://publisher.test/data?q={q}")
                context = QueryContext(http_client=client)
                with mock.patch.object(QueryContext, "sleep", mock.AsyncMock()) as sleep:
                    result = await okf_fetch.fetch_async(
                        path, "get", {"q": "hello world"}, context=context)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, 2)
        sleep.assert_awaited_once_with(1.5)

    async def test_post_body_and_credential_error_preserve_semantics(self):
        seen = {}
        def handler(request):
            seen.update(method=request.method, body=request.content, content_type=request.headers["content-type"])
            return httpx.Response(200, text="Missing API key")
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                path = os.path.join(directory, "source.md")
                descriptor(path, "https://publisher.test/search", "POST", '{"query":"$q"}')
                with self.assertRaisesRegex(SystemExit, "CREDENTIAL_ERROR"):
                    await okf_fetch.fetch_async(path, "get", {"q": "Stanford"},
                                                context=QueryContext(http_client=client))
        self.assertEqual(seen, {"method": "POST", "body": b'{"query":"Stanford"}',
                                "content_type": "application/json"})

    async def test_publisher_429_honors_retry_after_and_surfaces_rate_limit(self):
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "0.25"})
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                path = os.path.join(directory, "source.md")
                descriptor(path, "https://publisher.test/data")
                with mock.patch.object(QueryContext, "sleep", mock.AsyncMock()) as sleep, \
                     self.assertRaises(okf_fetch.PublisherRateLimitError):
                    await okf_fetch.fetch_async(
                        path, "get", context=QueryContext(http_client=client), tries=2)
        self.assertEqual(calls, 2)
        sleep.assert_awaited_once_with(0.25)

    async def test_publisher_429_larger_than_query_window_is_reported_without_sleeping(self):
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "11057"})
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                path = os.path.join(directory, "source.md")
                descriptor(path, "https://publisher.test/data")
                context = QueryContext.with_timeout(180, http_client=client)
                with mock.patch.object(QueryContext, "sleep", mock.AsyncMock()) as sleep, \
                     self.assertRaisesRegex(okf_fetch.PublisherRateLimitError,
                                           "temporarily rate limiting"):
                    await okf_fetch.fetch_async(path, "get", context=context)
        self.assertEqual(calls, 1)
        sleep.assert_not_awaited()

    async def test_cancellation_closes_pending_http_request(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=Transport()) as client:
                path = os.path.join(directory, "source.md")
                descriptor(path, "https://publisher.test/data")
                context = QueryContext(http_client=client)
                task = asyncio.create_task(okf_fetch.fetch_async(path, "get", context=context))
                await started.wait()
                context.cancel()
                with self.assertRaises(runtime.QueryCancelled):
                    await task
        self.assertTrue(cancelled.is_set())

    async def test_driver_accessor_is_native_and_has_async_twin(self):
        with mock.patch.object(okf_fetch, "fetch", return_value={"sync": True}) as sync:
            self.assertEqual(driver.accessor("sources/x.md", "get", q="x"), {"sync": True})
        sync.assert_called_once()
        with mock.patch.object(okf_fetch, "fetch_async", mock.AsyncMock(return_value={"async": True})):
            result = await driver.accessor_async(
                "sources/x.md", "get", q="x", context=QueryContext(http_client=object()))
        self.assertEqual(result, {"async": True})

    async def test_regression_fanout_spawns_zero_accessor_processes(self):
        with mock.patch.object(okf_fetch, "fetch", return_value={"ok": True}) as fetch:
            results = [driver.accessor("sources/x.md", "get", q=index) for index in range(169)]
        self.assertEqual(len(results), 169)
        self.assertEqual(fetch.call_count, 169)
        self.assertFalse(hasattr(driver, "subprocess"))

    async def test_descriptor_preload_populates_the_shared_frontmatter_cache(self):
        okf_fetch.load_okf.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            descriptor(os.path.join(directory, "one.md"), "https://one.test")
            descriptor(os.path.join(directory, "two.md"), "https://two.test")
            self.assertEqual(okf_fetch.preload_descriptors(directory), 2)
            info = okf_fetch.load_okf.cache_info()
        self.assertEqual((info.misses, info.currsize), (2, 2))

    async def test_new_async_source_path_contains_no_blocking_compatibility_calls(self):
        functions = [okf_fetch.fetch_async, resolver._get_async, driver.fetch_metric_async,
                     bq.AsyncBigQueryClient.query, grants.AsyncGrantPool.query]
        for function in functions:
            source = inspect.getsource(function)
            for banned in ("urllib.request", "requests.", "time.sleep", "subprocess"):
                self.assertNotIn(banned, source, (function.__qualname__, banned))


class AsyncResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_claims_and_hierarchy_use_shared_http_client(self):
        def handler(request):
            query = request.url.params
            if query.get("action") == "wbsearchentities":
                return httpx.Response(200, json={"search": [{"id": "Q1", "label": "Detroit"}]})
            qid = query["ids"]
            claims = {"P774": [{"mainsnak": {"datavalue": {"value": "26-22000"}}}]}
            if qid == "Q1":
                claims["P131"] = [{"mainsnak": {"datavalue": {"value": {"id": "Q2"}}}}]
            return httpx.Response(200, json={"entities": {qid: {
                "labels": {"en": {"value": "Detroit" if qid == "Q1" else "Michigan"}},
                "claims": claims}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            context = QueryContext(http_client=client)
            with mock.patch.object(resolver, "_cache", {}):
                candidates = await resolver.search_async("Detroit", context=context)
                label, keys = await resolver.claims_async("Q1", context=context)
                levels = await resolver.hierarchy_async("Q1", context=context)
        self.assertEqual(candidates[0]["id"], "Q1")
        self.assertEqual((label, keys["fips_place"]), ("Detroit", "26-22000"))
        self.assertEqual([level["qid"] for level in levels], ["Q1", "Q2"])


class AsyncSecTests(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_same_cik_fetches_companyfacts_once(self):
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"entityName": "Example", "facts": {}})
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            sec = driver.AsyncSecClient(http, requests_per_second=10000)
            first, second = QueryContext(http_client=http), QueryContext(http_client=http)
            values = await asyncio.gather(sec.company_facts("1", first), sec.company_facts("1", second))
        self.assertEqual(calls, 1)
        self.assertEqual(values[0]["entityName"], "Example")

    async def test_final_429_is_reported_as_source_rate_limit(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(429, headers={"Retry-After": "0"}))) as http:
            sec = driver.AsyncSecClient(http, requests_per_second=10000)
            context = QueryContext(http_client=http)
            with self.assertRaises(driver.SourceRateLimitError):
                await sec._json("https://data.sec.gov/test", context, attempts=1)

    async def test_async_metric_fetch_uses_companyfacts_and_async_finder(self):
        class Sec:
            async def company_facts(self, cik, context):
                return {"entityName": "Microsoft Corporation", "facts": {"us-gaap": {
                    "NetIncomeLoss": {"units": {"USD": [{
                        "form": "10-K", "start": "2022-07-01", "end": "2023-06-30",
                        "val": 72361000000}]}}}}}
        context = QueryContext(sec_client=Sec())
        hit = {"identifier": "sources/sec-edgar/net-income-loss.md", "title": "Net Income"}
        with mock.patch.object(driver.ard_client, "search_async",
                               mock.AsyncMock(return_value=[hit])) as search:
            result = await driver.fetch_metric_async(
                "profitability", cik="789019", period="FY2023", log=False, context=context)
        self.assertEqual(result["value"], 72361000000)
        self.assertEqual(result["concept"], "us-gaap:NetIncomeLoss")
        search.assert_awaited_once()

    async def test_async_concept_selection_never_calls_sync_llm(self):
        reported = [(0, {"concept": "us-gaap:A", "period": "FY2023", "period_end": "2023-12-31",
                         "value": 10, "unit": "USD"}),
                    (1, {"concept": "us-gaap:B", "period": "FY2023", "period_end": "2023-12-31",
                         "value": 20, "unit": "USD"})]
        with mock.patch.object(driver.llm, "chat_async", mock.AsyncMock(
                return_value='{"i":1,"dominant":true,"alternatives":[]}')) as chat, \
             mock.patch.object(driver, "ask_llm", side_effect=AssertionError("sync LLM called")):
            result = await driver._pick_by_data_async(
                "special metric", reported, QueryContext(), log=False)
        self.assertEqual(result["concept"], "us-gaap:B")
        chat.assert_awaited_once()


class AsyncBigQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_and_repeated_values_fail_instead_of_returning_wrong_shape(self):
        with self.assertRaisesRegex(RuntimeError, "nested or repeated"):
            bq.AsyncBigQueryClient._value({"f": []}, "RECORD")
        with self.assertRaisesRegex(RuntimeError, "nested or repeated"):
            bq.AsyncBigQueryClient._value(["1", "2"], "INT64")

    async def test_job_polling_pagination_location_and_numeric_conversion(self):
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path, dict(request.url.params)))
            if request.method == "POST":
                return httpx.Response(200, json={"status": {"state": "RUNNING"}})
            if "/jobs/" in request.url.path:
                return httpx.Response(200, json={"status": {"state": "DONE"}})
            if not request.url.params.get("pageToken"):
                return httpx.Response(200, json={"schema": {"fields": [
                    {"name": "n", "type": "INT64"}, {"name": "v", "type": "NUMERIC"}]},
                    "rows": [{"f": [{"v": "3"}, {"v": "1.5"}]}], "pageToken": "next"})
            return httpx.Response(200, json={"rows": [{"f": [{"v": "4"}, {"v": "2.5"}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = bq.AsyncBigQueryClient("project", http, location="EU")
            client.token, client.token_expires_at = "token", 10**12
            context = QueryContext(http_client=http, bigquery_client=client)
            with mock.patch.object(QueryContext, "sleep", mock.AsyncMock()):
                rows = await client.query("SELECT 1", context=context)
        self.assertEqual(rows, [{"n": 3, "v": 1.5}, {"n": 4, "v": 2.5}])
        self.assertTrue(all(params.get("location") == "EU" for method, path, params in calls
                            if method == "GET"))

    async def test_expired_service_account_token_refresh_is_shared(self):
        token_calls = 0
        async def handler(request):
            nonlocal token_calls
            token_calls += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        credentials = SimpleNamespace(service_account_email="x@example.test", _token_uri="https://oauth.test/token",
                                      signer=object())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = bq.AsyncBigQueryClient("project", http, credentials_file="fixture.json")
            context = QueryContext(http_client=http)
            with mock.patch("google.oauth2.service_account.Credentials.from_service_account_file",
                            return_value=credentials), mock.patch("google.auth.jwt.encode", return_value=b"jwt"):
                tokens = await asyncio.gather(client._access_token(context), client._access_token(context))
        self.assertEqual(tokens, ["fresh", "fresh"])
        self.assertEqual(token_calls, 1)

    async def test_cancellation_issues_jobs_cancel(self):
        inserted, cancelled = asyncio.Event(), asyncio.Event()
        async def handler(request):
            if request.url.path.endswith("/cancel"):
                cancelled.set()
                return httpx.Response(200, json={})
            if request.method == "POST":
                inserted.set()
                return httpx.Response(200, json={"status": {"state": "RUNNING"}})
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = bq.AsyncBigQueryClient("project", http)
            client.token, client.token_expires_at = "token", 10**12
            context = QueryContext(http_client=http)
            task = asyncio.create_task(client.query("SELECT 1", context=context))
            await inserted.wait()
            context.cancel()
            with self.assertRaises(runtime.QueryCancelled):
                await task
        self.assertTrue(cancelled.is_set())

    async def test_unauthorized_response_refreshes_token_once(self):
        authorizations = []
        def handler(request):
            authorizations.append(request.headers.get("authorization"))
            return httpx.Response(401 if len(authorizations) == 1 else 200,
                                  json={"status": {"state": "DONE"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = bq.AsyncBigQueryClient("project", http)
            client.token, client.token_expires_at = "expired", 10**12
            context = QueryContext(http_client=http)
            with mock.patch.object(client, "_access_token", mock.AsyncMock(
                    side_effect=["expired", "fresh"])):
                payload = await client._request(context, "GET", "/fixture")
        self.assertEqual(payload["status"]["state"], "DONE")
        self.assertEqual(authorizations, ["Bearer expired", "Bearer fresh"])


class AsyncGrantTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_opens_lazily_once(self):
        opens = 0
        class Pool:
            async def open(self, wait):
                nonlocal opens
                opens += 1
            async def close(self): pass
        grant_pool = grants.AsyncGrantPool.__new__(grants.AsyncGrantPool)
        grant_pool._pool_class = lambda *args, **kwargs: Pool()
        grant_pool._pool_args = ("postgresql://fixture", 1, 2)
        grant_pool._open_lock = asyncio.Lock()
        grant_pool.pool = None
        context = QueryContext()
        await asyncio.gather(grant_pool.open(context=context), grant_pool.open(context=context))
        self.assertEqual(opens, 1)

    async def test_failed_lazy_open_closes_pool_and_can_retry(self):
        pools = []
        class Pool:
            def __init__(self): self.closed = False
            async def open(self, wait): raise RuntimeError("unreachable")
            async def close(self): self.closed = True
        def factory(*args, **kwargs):
            pool = Pool()
            pools.append(pool)
            return pool
        grant_pool = grants.AsyncGrantPool.__new__(grants.AsyncGrantPool)
        grant_pool._pool_class = factory
        grant_pool._pool_args = ("postgresql://fixture", 1, 2)
        grant_pool._open_lock = asyncio.Lock()
        grant_pool.pool = None
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "unreachable"):
                await grant_pool.open(context=QueryContext())
        self.assertEqual(len(pools), 2)
        self.assertTrue(all(pool.closed for pool in pools))

    async def test_pool_query_translates_sql_and_normalizes_decimal(self):
        seen = {}
        class Cursor:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def execute(self, sql, params): seen.update(sql=sql, params=params)
            async def fetchall(self): return [(decimal.Decimal("12"), decimal.Decimal("1.5"))]
        class Connection:
            def cursor(self): return Cursor()
        class Lease:
            async def __aenter__(self): return Connection()
            async def __aexit__(self, *_): pass
        class Pool:
            def connection(self): return Lease()
        grant_pool = grants.AsyncGrantPool.__new__(grants.AsyncGrantPool)
        grant_pool.pool = Pool()
        grant_pool._open_lock = asyncio.Lock()
        rows = await grant_pool.query("SELECT * FROM x WHERE name LIKE ?", ("A",),
                                      context=QueryContext())
        self.assertEqual(seen, {"sql": "SELECT * FROM x WHERE name ILIKE %s", "params": ("A",)})
        self.assertEqual(rows, [(12, 1.5)])

    async def test_server_mode_rejects_sqlite_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             self.assertRaisesRegex(RuntimeError, "SQLite is offline/CLI-only"):
            grants.AsyncGrantPool()

    async def test_top_grantmaker_result_matches_sync_shape(self):
        class Pool:
            async def query(self, sql, params, context):
                return [("FORD FOUNDATION", 1000, 2), ("SECOND", 500, 1)]
        context = QueryContext(grant_pool=Pool())
        result = await grants.top_grantmakers_async(2, context=context)
        self.assertEqual(result, {
            "measure": "total granted", "complete": True,
            "ranking": [
                {"label": "FORD FOUNDATION", "entity": "grantmaker/FORD FOUNDATION",
                 "value": 1000, "value_display": "$1,000", "grants": 2},
                {"label": "SECOND", "entity": "grantmaker/SECOND", "value": 500,
                 "value_display": "$500", "grants": 1}],
            "top": {"label": "FORD FOUNDATION", "entity": "grantmaker/FORD FOUNDATION",
                    "value": 1000, "value_display": "$1,000", "grants": 2},
            "source": grants.SOURCE})

    async def test_every_server_grant_operation_has_an_async_dispatch(self):
        expected = {"forward", "reverse", "top_grantmakers", "biggest_recipients",
                    "funders_above", "overview", "geo", "grants_by_cause", "shared_grantees"}
        self.assertEqual(set(grants.ASYNC_OPERATIONS), expected)


class SourceClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_preloads_binds_and_shutdown_closes_owned_resources(self):
        class Http:
            def __init__(self): self.closed = False
            async def aclose(self): self.closed = True
        class Pool:
            def __init__(self): self.opened = self.closed = False
            async def open(self): self.opened = True
            async def close(self): self.closed = True
        http, pool = Http(), Pool()
        with mock.patch.object(source_clients.okf_fetch, "preload_descriptors", return_value=10425), \
             mock.patch.object(source_clients.ard_client, "create_async_http_client", return_value=http), \
             mock.patch.object(source_clients.grants, "AsyncGrantPool", return_value=pool), \
             mock.patch.dict(os.environ, {"GRANTS_URL": "postgresql://fixture"}, clear=True):
            clients = await source_clients.AsyncSourceClients().start()
            context = clients.bind(QueryContext())
            self.assertIs(context.http_client, http)
            self.assertIs(context.sec_client, clients.sec)
            self.assertIs(context.grant_pool, pool)
            self.assertEqual(clients.descriptor_count, 10425)
            await clients.close()
        self.assertFalse(pool.opened)
        self.assertTrue(pool.closed)
        self.assertTrue(http.closed)

    async def test_partial_startup_failure_closes_owned_http_client(self):
        class Http:
            def __init__(self): self.closed = False
            async def aclose(self): self.closed = True
        http = Http()
        with mock.patch.object(source_clients.okf_fetch, "preload_descriptors", return_value=1), \
             mock.patch.object(source_clients.ard_client, "create_async_http_client", return_value=http), \
             mock.patch.object(source_clients.driver, "AsyncSecClient", side_effect=RuntimeError("bad sec")):
            with self.assertRaisesRegex(RuntimeError, "bad sec"):
                await source_clients.AsyncSourceClients().start()
        self.assertTrue(http.closed)

    async def test_unreachable_optional_grants_pool_is_not_opened_at_startup(self):
        class Pool:
            def __init__(self): self.opened = False
            async def open(self):
                self.opened = True
                raise RuntimeError("unreachable")
            async def close(self): pass
        pool = Pool()
        with mock.patch.object(source_clients.okf_fetch, "preload_descriptors", return_value=1), \
             mock.patch.object(source_clients.grants, "AsyncGrantPool", return_value=pool), \
             mock.patch.dict(os.environ, {"GRANTS_URL": "postgresql://unreachable"}, clear=True):
            clients = await source_clients.AsyncSourceClients().start()
            await clients.close()
        self.assertFalse(pool.opened)


if __name__ == "__main__":
    unittest.main()
