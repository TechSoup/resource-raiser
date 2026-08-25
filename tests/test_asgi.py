"""Stage 7: one-loop ASGI JSON/SSE contract, isolation, cancellation, and health."""
import asyncio
import json
import os
import sys
import threading
import unittest
from unittest import mock

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app as asgi
import nlweb
import runtime
from query_context import ProviderPermits


class Http:
    async def post(self, *args, **kwargs):
        return None


class Clients:
    def __init__(self):
        self.http = Http(); self.descriptor_count = 10425; self.grants = None
        self.sec = None
        self.permits = ProviderPermits({"llm": 8, "finder": 8})

    async def start(self): return self
    async def close(self): pass
    def bind(self, context):
        context.http_client = self.http; context.permits = self.permits
        return context


def result(question):
    return {"question": question, "answer": f"answer-{question}", "shape": "point",
            "plan": "fixture", "usage": {}, "discovery_usage": {}, "intent": {},
            "attempts": [], "evidence": {"value": question}, "answer_renderer": "point",
            "source": {}, "candidates": [], "data": {"value": question}}


class AsgiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def engine(question, context=None, **kwargs):
            await context.emit("status", icon="x", msg=f"progress-{question}")
            await asyncio.sleep(.005)
            return result(question)
        self.application = asgi.create_app(engine=engine, clients_factory=Clients)
        self.life = self.application.router.lifespan_context(self.application)
        with mock.patch("harness._sources_catalog", return_value=[]):
            await self.life.__aenter__()
        self.health = mock.patch("ard_client.health_async",
                                 mock.AsyncMock(return_value={"ok": True, "entries": 10425}))
        self.health.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.application), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose(); self.health.stop(); await self.life.__aexit__(None, None, None)

    async def test_json_and_named_sse_preserve_nlweb_order(self):
        response = await self.client.post("/ask", json={"query": "A", "streaming": False})
        self.assertEqual(response.status_code, 200)
        messages = response.json()["messages"]
        self.assertEqual([item["message_type"] for item in messages],
            [nlweb.BEGIN, nlweb.INTERMEDIATE, nlweb.NLWS, nlweb.COMPLETE, nlweb.END])
        response = await self.client.get("/ask?query=B&sse_format=named")
        events = [line.removeprefix("event: ") for line in response.text.splitlines()
                  if line.startswith("event: ")]
        self.assertEqual(events,
            [nlweb.BEGIN, nlweb.INTERMEDIATE, nlweb.NLWS, nlweb.COMPLETE, nlweb.END])

    async def test_homepage_has_no_edit_and_rerun_control(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("edit & rerun", response.text.lower())
        self.assertNotIn("edit-intent", response.text)

    async def test_one_hundred_users_overlap_without_threads_or_crossed_answers(self):
        before = {thread.ident for thread in threading.enumerate()}
        responses = await asyncio.gather(*(self.client.post(
            "/ask", json={"query": str(index), "streaming": False}) for index in range(100)))
        for index, response in enumerate(responses):
            content = next(item["content"] for item in response.json()["messages"]
                           if item["message_type"] == nlweb.NLWS)
            self.assertEqual(content["answer"], f"answer-{index}")
        after = {thread.ident for thread in threading.enumerate()}
        self.assertFalse(after - before, f"request path created threads: {after - before}")

    async def test_health_is_responsive_while_query_provider_is_stalled(self):
        gate = asyncio.Event()
        async def stalled(question, context=None, **kwargs):
            await gate.wait()
            return result(question)
        application = asgi.create_app(engine=stalled, clients_factory=Clients)
        life = application.router.lifespan_context(application)
        with mock.patch("harness._sources_catalog", return_value=[]):
            await life.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                   base_url="http://test")
        try:
            query = asyncio.create_task(client.post("/ask", json={"query": "slow", "streaming": False}))
            await asyncio.sleep(.01)
            response = await asyncio.wait_for(client.get("/healthz"), .25)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["active_requests"], 1)
            gate.set(); await query
        finally:
            await client.aclose(); await life.__aexit__(None, None, None)

    async def test_disconnect_cancels_root_and_all_child_work(self):
        cancelled = asyncio.Event()
        async def engine(question, context=None, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        spec = nlweb.parse_request({"query": "cancel"})
        async def disconnected(): return True
        stream = asgi.run_nlweb_async(spec, clients=Clients(), engine=engine,
                                      disconnect=disconnected)
        self.assertEqual((await anext(stream))["message_type"], nlweb.BEGIN)
        with self.assertRaises(asyncio.CancelledError):
            await anext(stream)
        await asyncio.wait_for(cancelled.wait(), 1)

    async def test_health_reports_operational_stats(self):
        response = await self.client.get("/healthz")
        payload = response.json()
        self.assertEqual(payload["tables"], 10425)
        for key in ("event_loop_lag_ms", "active_requests", "request_queue_depth",
                    "provider_permits", "telemetry", "uptime_seconds", "instance_id",
                    "azure_monitor", "sec_pacing"):
            self.assertIn(key, payload)

    async def test_telemetry_carries_instance_and_request_ids(self):
        response = await self.client.post("/ask", json={"query": "trace", "streaming": False})
        await asyncio.wait_for(self.application.state.telemetry.queue.join(), 1)
        event = self.application.state.telemetry.recent[-1]
        self.assertEqual(event["trace_id"], response.headers["X-Request-ID"])
        self.assertEqual(event["instance_id"], self.application.state.instance_id)

    async def test_azure_monitor_configuration_selects_only_application_logger(self):
        with mock.patch.dict(os.environ, {"APPLICATIONINSIGHTS_CONNECTION_STRING": "fixture"}), \
             mock.patch("azure.monitor.opentelemetry.configure_azure_monitor") as configure:
            self.assertTrue(asgi.configure_azure_monitor())
        configure.assert_called_once_with(
            connection_string="fixture", logger_name="resource_raiser")

    async def test_azure_request_event_is_written_by_bounded_exporter_task(self):
        exporter = asgi.AsyncTelemetryExporter(Http(), azure_monitor=True)
        with mock.patch.object(exporter.logger, "info") as info:
            await exporter.start()
            exporter.record({"trace_id": "trace-1"})
            await asyncio.wait_for(exporter.queue.join(), 1)
            await exporter.close()
        self.assertIn('"trace_id":"trace-1"', info.call_args.args[0])

    async def test_azure_mode_wraps_starlette_with_asgi_instrumentation(self):
        with mock.patch.object(asgi, "AZURE_MONITOR_ENABLED", True):
            application = asgi.create_app(engine=mock.AsyncMock(), clients_factory=Clients)
        self.assertIn("OpenTelemetryMiddleware",
                      [item.cls.__name__ for item in application.user_middleware])

    async def test_ordinary_refusal_does_not_kill_server(self):
        async def refuses(question, context=None, **kwargs):
            raise runtime.Refused("ordinary refusal")
        application = asgi.create_app(engine=refuses, clients_factory=Clients)
        life = application.router.lifespan_context(application)
        with mock.patch("harness._sources_catalog", return_value=[]):
            await life.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                   base_url="http://test")
        try:
            response = await client.post("/ask", json={"query": "refuse", "streaming": False})
            self.assertEqual(response.status_code, 200)
            messages = response.json()["messages"]
            self.assertEqual(messages[-2]["message_type"], nlweb.ERROR)
            self.assertEqual(messages[-2]["content"], "ordinary refusal")
            health = await client.get("/healthz")
            self.assertEqual(health.status_code, 200)
        finally:
            await client.aclose(); await life.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
