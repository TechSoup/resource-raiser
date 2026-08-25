"""Single-loop ASGI transport for Resource Raiser.

Run one event loop per instance::

    uvicorn app:app --host 127.0.0.1 --port 8099 --workers 1

Scale with instances.  The engine, source pools, provider permits, progress queues, and request
cancellation all live on that loop; no request or query worker thread is created.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

def configure_azure_monitor():
    """Enable Azure Monitor's OpenTelemetry distribution when App Service supplies a connection."""
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor as configure
    except ImportError as exc:  # fail startup instead of silently losing production telemetry
        raise RuntimeError(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is set but azure-monitor-opentelemetry is missing"
        ) from exc
    configure(connection_string=connection_string, logger_name="resource_raiser")
    return True


# Azure's distro must instrument Starlette before the application class is imported/constructed.
AZURE_MONITOR_ENABLED = configure_azure_monitor()

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

import ard_client
import docpage
import driver
import harness
import nlweb
import runtime
from query_context import QueryContext
from source_clients import AsyncSourceClients


class AsyncTelemetryExporter:
    """Bounded request telemetry exported away from the request task."""
    def __init__(self, http, endpoint=None, max_queue=1024, azure_monitor=False):
        self.http = http
        self.endpoint = endpoint or os.getenv("TELEMETRY_EXPORT_URL")
        self.azure_monitor = azure_monitor
        self.logger = logging.getLogger("resource_raiser.telemetry")
        if azure_monitor:
            self.logger.setLevel(logging.INFO)
        self.queue = asyncio.Queue(maxsize=max_queue)
        self.recent = deque(maxlen=100)
        self.exported = self.dropped = 0
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._run(), name="telemetry-exporter")
        return self

    def record(self, event):
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _run(self):
        while True:
            event = await self.queue.get()
            try:
                if self.endpoint:
                    await self.http.post(self.endpoint, json=event, timeout=10)
                elif self.azure_monitor:
                    # Azure Monitor's logging instrumentation exports this record. It runs in the
                    # bounded exporter task, never in the latency-sensitive request task.
                    self.logger.info(json.dumps({"event": "resource_raiser.request", **event},
                                                separators=(",", ":")))
                    self.recent.append(event)
                else:
                    self.recent.append(event)
                self.exported += 1
            except Exception:
                self.dropped += 1
            finally:
                self.queue.task_done()

    async def close(self):
        if self._task:
            try:
                await asyncio.wait_for(
                    self.queue.join(), float(os.getenv("TELEMETRY_DRAIN_SECONDS", "5")))
            except asyncio.TimeoutError:
                self.dropped += self.queue.qsize()
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def snapshot(self):
        return {"queue_depth": self.queue.qsize(), "queue_limit": self.queue.maxsize,
                "exported": self.exported, "dropped": self.dropped}


class LoopLag:
    def __init__(self, interval=.25):
        self.interval = interval
        self.current_ms = self.max_ms = 0.0
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._run(), name="event-loop-lag")
        return self

    async def _run(self):
        expected = time.monotonic() + self.interval
        while True:
            await asyncio.sleep(self.interval)
            now = time.monotonic()
            self.current_ms = max(0.0, (now - expected) * 1000)
            self.max_ms = max(self.max_ms, self.current_ms)
            expected = now + self.interval

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def _result_messages(stream, request, result):
    """The established NLWeb terminal contract, shared by JSON and SSE responses."""
    items = [nlweb.item(candidate) for candidate in result.get("candidates") or []
             if (candidate.get("score") or 0) >= request.get("min_score", 0)]
    items = items[:request.get("max_results", 10)]
    source = result.get("source") or {}
    if source.get("identifier"):
        chosen = nlweb.item({"identifier": source["identifier"], "title": source.get("title"),
            "publisher": source.get("publisher"), "score": 100,
            "description": result.get("plan", ""),
            "schema_object": driver.frontmatter(source["identifier"]) or {}})
        items = [chosen] + [item for item in items if item["url"] != chosen["url"]]
        items = items[:max(1, request.get("max_results", 10))]
    if items:
        yield stream.message(nlweb.RESULT, items)
    if request.get("mode") != "list":
        clarification = (result.get("clarification")
                         if result.get("status") == "needs_clarification" else None)
        content = {"@type": "ClarificationRequest" if clarification else "GeneratedAnswer",
            "items": items, "status": result.get("status") or "answered",
            "shape": result.get("shape"), "plan": result.get("plan"),
            "data": result.get("data"), "usage": result.get("usage"),
            "discovery_usage": result.get("discovery_usage"), "intent": result.get("intent"),
            "attempts": result.get("attempts") or [], "evidence": result.get("evidence"),
            "answer_renderer": result.get("answer_renderer")}
        if clarification:
            content.update({"question": clarification.get("question"),
                            "original_query": result.get("question"),
                            "options": clarification.get("options") or []})
        else:
            content["answer"] = result.get("answer") or ""
        yield stream.message(nlweb.NLWS, content)
    if request.get("debug"):
        yield stream.message(nlweb.INTERMEDIATE, json.dumps({
            "shape": result.get("shape"), "plan": result.get("plan"),
            "usage": result.get("usage"), "discovery_usage": result.get("discovery_usage"),
            "data": result.get("data")})[:20000], "system")
    yield stream.message(nlweb.COMPLETE, "", "system")
    yield stream.message(nlweb.END, "", "system")


async def run_nlweb_async(spec, *, clients, engine=harness.run, disconnect=None,
                          progress_size=128, heartbeat_seconds=15):
    """Yield NLWeb messages and ``None`` heartbeats from one owned root task."""
    stream = nlweb.Stream(spec.get("conversation_id"))
    yield stream.message(nlweb.BEGIN, "", "system")
    context = clients.bind(QueryContext.with_timeout(
        float(os.getenv("QUERY_TIMEOUT_SECONDS", "180")),
        progress=asyncio.Queue(maxsize=progress_size)))
    async def invoke_engine():
        return await engine(
            spec["query"], sites=spec.get("sites") or None,
            assumptions=spec.get("assumptions") or None,
            on_ambiguity=spec.get("on_ambiguity") or "answer", context=context)

    task = asyncio.create_task(invoke_engine(), name=f"query-{context.trace_id}")
    try:
        await asyncio.sleep(0)  # make the root task own any children before disconnect can cancel it
        last_frame = time.monotonic()
        while not task.done():
            if disconnect and await disconnect():
                context.cancel(); task.cancel()
                raise asyncio.CancelledError
            progress = asyncio.create_task(context.progress.get())
            done, _ = await asyncio.wait({task, progress}, timeout=min(.25, heartbeat_seconds),
                                         return_when=asyncio.FIRST_COMPLETED)
            if progress in done:
                line = harness._nlweb_text(progress.result())
                if line:
                    last_frame = time.monotonic()
                    yield stream.message(nlweb.INTERMEDIATE, line, "system")
            else:
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)
            if not done and time.monotonic() - last_frame >= heartbeat_seconds:
                last_frame = time.monotonic()
                yield None
        result = await task
        while not context.progress.empty():
            line = harness._nlweb_text(context.progress.get_nowait())
            if line:
                yield stream.message(nlweb.INTERMEDIATE, line, "system")
        for message in _result_messages(stream, spec, result):
            yield message
    except (runtime.Refused, driver.SourceRateLimitError, runtime.QueryCancelled) as exc:
        yield stream.message(nlweb.ERROR, str(exc), "system")
        yield stream.message(nlweb.END, "", "system")
    except asyncio.CancelledError:
        context.cancel(); task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception as exc:
        yield stream.message(nlweb.ERROR, f"{type(exc).__name__}: {exc}", "system")
        yield stream.message(nlweb.END, "", "system")
    finally:
        if not task.done():
            context.cancel(); task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def create_app(engine=harness.run, clients_factory=AsyncSourceClients):
    @asynccontextmanager
    async def lifespan(application):
        application.state.started = time.time()
        application.state.instance_id = os.getenv("WEBSITE_INSTANCE_ID") or uuid.uuid4().hex[:12]
        application.state.azure_monitor = AZURE_MONITOR_ENABLED
        application.state.clients = await clients_factory().start()
        application.state.telemetry = await AsyncTelemetryExporter(
            application.state.clients.http,
            azure_monitor=application.state.azure_monitor).start()
        application.state.lag = await LoopLag().start()
        application.state.active = 0
        application.state.progress_queues = 0
        application.state.quota = {}
        application.state.totals = {"questions": 0, "llm_calls": 0, "total_tokens": 0,
            "cost_usd": 0.0, "discovery_calls": 0, "discovery_tokens": 0,
            "discovery_cost_usd": 0.0, "by_stage": {}, "by_model": {}}
        application.state.request_slots = asyncio.Semaphore(
            int(os.getenv("MAX_CONCURRENT_QUERIES", "100")))
        application.state.sources = harness._sources_catalog()
        try:
            yield
        finally:
            await application.state.lag.close()
            await application.state.telemetry.close()
            await application.state.clients.close()

    async def parse(request):
        if request.method == "GET":
            params = dict(request.query_params)
        else:
            limit = int(os.getenv("HARNESS_MAX_BODY", "65536"))
            body = await request.body()
            if len(body) > limit:
                return None, JSONResponse({"error": f"request body exceeds {limit} bytes"}, 413)
            try:
                params = json.loads(body or b"{}")
            except ValueError:
                return None, JSONResponse({"error": "invalid JSON body"}, 400)
            if not isinstance(params, dict):
                params = {}
        spec = nlweb.parse_request(params)
        if not spec["query"]:
            return None, JSONResponse({"error": "missing 'query'"}, 400)
        if spec.get("assumptions_error"):
            return None, JSONResponse({"error": spec["assumptions_error"]}, 400)
        return spec, None

    async def ask(request):
        spec, error = await parse(request)
        if error:
            return error
        state, trace_id, started = request.app.state, uuid.uuid4().hex, time.monotonic()
        quota_limit = int(os.getenv("ASK_LIMIT_PER_DAY", "200"))
        if quota_limit:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            client = request.client.host if request.client else "unknown"
            if os.getenv("TRUST_PROXY", "0").lower() in ("1", "true", "yes"):
                client = request.headers.get("x-forwarded-for", client).split(",", 1)[0].strip()
            quota_day, used = state.quota.get(client, (day, 0))
            if quota_day != day:
                used = 0
            if used >= quota_limit:
                return JSONResponse({"error": f"daily limit reached: {quota_limit} queries per day per source",
                                     "retry_after_seconds": 86400}, 429,
                                    headers={"Retry-After": "86400", "X-Request-ID": trace_id})
            state.quota[client] = (day, used + 1)
        try:
            await asyncio.wait_for(state.request_slots.acquire(), timeout=.001)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "server is at its concurrent query limit"}, 503,
                                headers={"Retry-After": "1", "X-Request-ID": trace_id})
        state.active += 1
        state.progress_queues += 1

        async def messages():
            terminal = "error"
            answer_content = {}
            try:
                async for message in run_nlweb_async(
                        spec, clients=state.clients, engine=engine,
                        disconnect=request.is_disconnected):
                    if message is None:
                        yield None
                    else:
                        if message["message_type"] == nlweb.NLWS and isinstance(message.get("content"), dict):
                            answer_content = message["content"]
                        if message["message_type"] == nlweb.COMPLETE:
                            terminal = "complete"
                        yield message
            except asyncio.CancelledError:
                terminal = "disconnected"
                raise
            finally:
                state.active -= 1
                state.progress_queues -= 1
                state.request_slots.release()
                usage, discovery = answer_content.get("usage") or {}, answer_content.get("discovery_usage") or {}
                totals = state.totals
                totals["questions"] += 1
                totals["llm_calls"] += usage.get("llm_calls", 0)
                totals["total_tokens"] += usage.get("total_tokens", 0)
                totals["cost_usd"] += usage.get("cost_usd", 0.0)
                totals["discovery_calls"] += discovery.get("llm_calls", 0)
                totals["discovery_tokens"] += discovery.get("total_tokens", 0)
                totals["discovery_cost_usd"] += discovery.get("cost_usd", 0.0)
                for field in ("by_stage", "by_model"):
                    for key, value in (usage.get(field) or {}).items():
                        bucket = totals[field].setdefault(
                            key, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
                        bucket["calls"] += value.get("calls", 0)
                        bucket["tokens"] += value.get("tokens", 0)
                        bucket["cost_usd"] += value.get("cost_usd", 0.0)
                state.telemetry.record({"trace_id": trace_id, "instance_id": state.instance_id,
                    "status": terminal,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "streaming": spec["streaming"], "usage": usage,
                    "discovery_usage": discovery})

        if not spec["streaming"]:
            collected = []
            async for message in messages():
                if message is not None:
                    collected.append(message)
            return JSONResponse({"messages": collected}, headers={"X-Request-ID": trace_id})

        async def frames():
            async for message in messages():
                if message is None:
                    yield b": heartbeat\n\n"
                else:
                    yield nlweb.encode(message, named=spec["named_events"])
        return StreamingResponse(frames(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
                     "X-Request-ID": trace_id})

    async def healthz(request):
        state = request.app.state
        context = state.clients.bind(QueryContext.with_timeout(2))
        try:
            finder_health = await ard_client.health_async(context=context)
        except Exception:
            finder_health = {"ok": False}
        grant_stats = {}
        pool = getattr(state.clients.grants, "pool", None)
        if pool is not None and hasattr(pool, "get_stats"):
            grant_stats = pool.get_stats()
        healthy = bool(finder_health.get("ok")) and state.clients.descriptor_count > 0
        sec = getattr(state.clients, "sec", None)
        payload = {"ok": healthy, "tables": int(finder_health.get("entries") or
                   state.clients.descriptor_count), "descriptors_loaded": state.clients.descriptor_count,
            "agent_finder": bool(finder_health.get("ok")),
            "uptime_seconds": round(time.time() - state.started),
            "instance_id": state.instance_id, "active_requests": state.active,
            "request_queue_depth": state.progress_queues,
            "event_loop_lag_ms": round(state.lag.current_ms, 2),
            "event_loop_lag_max_ms": round(state.lag.max_ms, 2),
            "provider_permits": state.clients.permits.snapshot(), "grant_pool": grant_stats,
            "telemetry": state.telemetry.snapshot(),
            "azure_monitor": state.azure_monitor,
            "sec_pacing": sec.snapshot() if sec and hasattr(sec, "snapshot") else {}}
        return JSONResponse(payload, 200 if healthy else 503)

    async def health(request):
        state = request.app.state
        return JSONResponse({"status": "ok", "uptime_seconds": round(time.time() - state.started),
            "instance_id": state.instance_id, "active_queries": state.active,
            "max_concurrent_queries": int(os.getenv("MAX_CONCURRENT_QUERIES", "100")),
            "saturated": state.request_slots.locked()})

    async def costs(request):
        totals = json.loads(json.dumps(request.app.state.totals))
        count = max(1, totals["questions"])
        totals["avg_cost_per_question_usd"] = round(
            (totals["cost_usd"] + totals["discovery_cost_usd"]) / count, 6)
        totals["combined_cost_usd"] = round(
            totals["cost_usd"] + totals["discovery_cost_usd"], 6)
        totals["cost_usd"] = round(totals["cost_usd"], 6)
        totals["discovery_cost_usd"] = round(totals["discovery_cost_usd"], 6)
        totals["note"] = "since process start; discovery cost is reported separately"
        return JSONResponse(totals)

    async def static(request):
        path = request.path_params.get("path", "")
        if path == "": return HTMLResponse(harness.PAGE)
        if path == "techsoup": return HTMLResponse(harness.TECHSOUP_PAGE)
        if path in ("how-it-works", "how"): return HTMLResponse(harness.HOW_PAGE)
        if path in ("ard",): return HTMLResponse(harness.ARD_PAGE)
        if path in ("life-of-a-query", "loq"):
            page = docpage.markdown_page("LIFE_OF_A_QUERY.md", "The life of a query",
                "How one question becomes an answer, a clarification, or a refusal")
            return HTMLResponse(page) if page else JSONResponse({"error": "not deployed"}, 404)
        return JSONResponse({"error": "not found"}, 404)

    async def sources(request):
        return JSONResponse({"sources": request.app.state.sources, "tabs": harness.EXAMPLE_TABS})

    async def techsoup_sources(request):
        directories = {directory for tab in harness.TECHSOUP_TABS for directory in tab["dirs"]}
        return JSONResponse({"sources": [item for item in request.app.state.sources
                                         if item["dir"] in directories],
                             "tabs": harness.TECHSOUP_TABS})

    async def sites(request):
        return JSONResponse({"message_type": "sites",
                             "sites": [item["dir"] for item in request.app.state.sources]})

    async def ard_manifest(request):
        context = request.app.state.clients.bind(QueryContext.with_timeout(10))
        return JSONResponse(await ard_client.manifest_async(context=context))

    async def ard_publishers(request):
        context = request.app.state.clients.bind(QueryContext.with_timeout(10))
        data = await ard_client.explore_async("publisher", context=context)
        facet = (data.get("facets") or {}).get("publisher") or {}
        return JSONResponse({"publishers": [{"dir": item["value"], "count": item["count"]}
            for item in facet.get("buckets", [])]})

    async def ard_entry(request):
        context = request.app.state.clients.bind(QueryContext.with_timeout(10))
        entry = await ard_client.entry_async(request.query_params.get("id", ""), context=context)
        if not entry: return JSONResponse({"error": "no such entry"}, 404)
        return JSONResponse({"identifier": entry["identifier"], "source": entry.get("publisher", ""),
            "ard_entry": entry, "raw": (entry.get("data") or {}).get("content", ""),
            "access_doc": entry.get("accessDescriptor", "")})

    async def ard_list(request):
        try:
            page = max(1, int(request.query_params.get("page", 1)))
            per = max(1, min(int(request.query_params.get("per", 50)), 100))
        except ValueError:
            return JSONResponse({"error": "page and per must be integers"}, 400)
        context = request.app.state.clients.bind(QueryContext.with_timeout(20))
        token = ""
        for _ in range(page - 1):
            data = await ard_client.agents_async(request.query_params.get("source", ""),
                request.query_params.get("q", ""), per, token, context=context)
            token = data.get("pageToken") or ""
            if not token: break
        data = await ard_client.agents_async(request.query_params.get("source", ""),
            request.query_params.get("q", ""), per, token, context=context)
        total = data.get("totalSize", 0)
        return JSONResponse({"source": request.query_params.get("source", ""), "total": total,
            "page": page, "pages": max(1, (total + per - 1) // per), "per": per,
            "query": request.query_params.get("q", ""), "pageToken": data.get("pageToken"),
            "entries": [{"identifier": item["identifier"], "title": item.get("displayName", ""),
                "description": item.get("description", ""), "scope": item.get("scope", ""),
                "queries": (item.get("representativeQueries") or [])[:4]}
                for item in data.get("entries", [])]})

    routes = [Route("/ask", ask, methods=["GET", "POST"]), Route("/healthz", healthz),
        Route("/health", health), Route("/costs", costs), Route("/sources", sources),
        Route("/techsoup-sources", techsoup_sources), Route("/sites", sites),
        Route("/ard/manifest", ard_manifest), Route("/ard/publishers", ard_publishers),
        Route("/ard/entry", ard_entry), Route("/ard/list", ard_list),
        Route("/{path:path}", static)]
    application = Starlette(routes=routes, lifespan=lifespan)
    application.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                               allow_headers=["*"], expose_headers=["X-Request-ID", "Retry-After"])
    if AZURE_MONITOR_ENABLED:
        # Starlette is not one of Azure Monitor's bundled framework instrumentations. Its generic
        # ASGI middleware is therefore registered explicitly after the global providers exist.
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
        application.add_middleware(OpenTelemetryMiddleware)
    return application


app = create_app()
