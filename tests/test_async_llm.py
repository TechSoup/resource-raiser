"""Stage 1 contracts for explicit query ownership and asynchronous LLM I/O."""
import asyncio
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import llm
import runtime
from query_context import QueryContext


def response(content="ok", prompt=3, completion=2, vectors=None, cost=None):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                            cost=cost, model_extra={})
    if vectors is not None:
        return SimpleNamespace(usage=usage,
                               data=[SimpleNamespace(embedding=vector) for vector in vectors])
    return SimpleNamespace(usage=usage,
                           choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeClient:
    def __init__(self, chat_create=None, embed_create=None):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=chat_create))
        self.embeddings = SimpleNamespace(create=embed_create)
        self.closed = False

    async def close(self):
        self.closed = True


class AsyncLlmTests(unittest.IsolatedAsyncioTestCase):
    def patches(self, **extra):
        stack = [mock.patch.object(llm, "provider", return_value="openai"),
                 mock.patch.object(llm, "_openrouter", return_value=False)]
        for name, value in extra.items():
            stack.append(mock.patch.object(llm, name, value))
        return _PatchStack(stack)

    async def test_chat_success_json_mode_and_per_call_telemetry(self):
        seen = {}

        async def create(**kwargs):
            seen.update(kwargs)
            return response('{"answer": 1}', prompt=7, completion=4, cost=0.002)

        context = QueryContext(llm_client=FakeClient(chat_create=create))
        with self.patches():
            answer = await llm.chat_async("system", "user", context=context, json_mode=True,
                                          model="fixture-model", stage="classify", max_tokens=50)
        self.assertEqual(answer, '{"answer": 1}')
        self.assertEqual(seen["response_format"], {"type": "json_object"})
        self.assertEqual(seen["max_tokens"], 50)
        snapshot = context.usage_ledger.snapshot()
        self.assertEqual(snapshot["llm_calls"], 1)
        self.assertEqual(snapshot["total_tokens"], 11)
        self.assertEqual(snapshot["cost_usd"], 0.002)
        self.assertEqual(snapshot["call_events"][0]["target"], "openai")
        self.assertEqual(snapshot["call_events"][0]["stage"], "classify")
        self.assertEqual(snapshot["call_events"][0]["outcome"], "success")

    async def test_deadline_cancels_provider_work(self):
        provider_cancelled = asyncio.Event()

        async def create(**_):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise

        context = QueryContext.with_timeout(0.01, llm_client=FakeClient(chat_create=create))
        with self.patches():
            with self.assertRaisesRegex(runtime.QueryCancelled, "deadline exceeded"):
                await llm.chat_async("s", "u", context=context, model="fixture")
        self.assertTrue(provider_cancelled.is_set())
        self.assertTrue(context.cancelled.is_set())

    async def test_explicit_cancellation_cancels_provider_work(self):
        started, provider_cancelled = asyncio.Event(), asyncio.Event()

        async def create(**_):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise

        context = QueryContext(llm_client=FakeClient(chat_create=create))
        with self.patches():
            task = asyncio.create_task(llm.chat_async("s", "u", context=context, model="fixture"))
            await started.wait()
            context.cancel()
            with self.assertRaisesRegex(runtime.QueryCancelled, "query cancelled"):
                await task
        self.assertTrue(provider_cancelled.is_set())

    async def test_transient_error_retries_without_changing_billed_call_count(self):
        calls = 0

        async def create(**_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("temporarily unavailable")
            return response("recovered")

        context = QueryContext(llm_client=FakeClient(chat_create=create))
        sleep = mock.AsyncMock()
        with self.patches(_RETRIES=1), mock.patch.object(QueryContext, "sleep", sleep):
            answer = await llm.chat_async("s", "u", context=context, model="fixture")
        self.assertEqual(answer, "recovered")
        self.assertEqual(calls, 2)
        self.assertEqual(context.usage_ledger.snapshot()["llm_calls"], 1)
        self.assertEqual([e["outcome"] for e in context.usage_ledger.snapshot()["call_events"]],
                         ["error", "success"])
        sleep.assert_awaited_once_with(1.0)

    async def test_429_honors_retry_after(self):
        class RateLimited(Exception):
            status_code = 429
            response = SimpleNamespace(headers={"Retry-After": "0.25"})

        replies = iter((RateLimited("rate limit"), response("ok")))

        async def create(**_):
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return value

        context = QueryContext(llm_client=FakeClient(chat_create=create))
        sleep = mock.AsyncMock()
        with self.patches(_RETRIES=1), mock.patch.object(QueryContext, "sleep", sleep):
            self.assertEqual(await llm.chat_async("s", "u", context=context, model="fixture"), "ok")
        sleep.assert_awaited_once_with(0.25)
        self.assertEqual(context.usage_ledger.snapshot()["call_events"][0]["outcome"],
                         "rate_limited")

    async def test_cancellation_interrupts_retry_backoff(self):
        attempted = asyncio.Event()

        async def create(**_):
            attempted.set()
            raise ConnectionError("temporarily unavailable")

        context = QueryContext(llm_client=FakeClient(chat_create=create))
        with self.patches(_RETRIES=3), mock.patch.object(llm, "_retry_after", return_value=60.0):
            task = asyncio.create_task(llm.chat_async("s", "u", context=context, model="fixture"))
            await attempted.wait()
            await asyncio.sleep(0)
            context.cancel()
            with self.assertRaisesRegex(runtime.QueryCancelled, "query cancelled"):
                await asyncio.wait_for(task, 0.2)
        self.assertEqual(len(context.usage_ledger.snapshot()["call_events"]), 1)

    async def test_simultaneous_tasks_keep_ledgers_isolated(self):
        ready = asyncio.Event()
        waiting = 0

        async def create(**kwargs):
            nonlocal waiting
            waiting += 1
            if waiting == 2:
                ready.set()
            await ready.wait()
            user = kwargs["messages"][1]["content"]
            tokens = 10 if user == "A" else 20
            return response(user, prompt=tokens, completion=1)

        shared = FakeClient(chat_create=create)
        first, second = QueryContext(llm_client=shared), QueryContext(llm_client=shared)
        with self.patches():
            answers = await asyncio.gather(
                llm.chat_async("s", "A", context=first, model="fixture", stage="first"),
                llm.chat_async("s", "B", context=second, model="fixture", stage="second"))
        self.assertEqual(answers, ["A", "B"])
        self.assertEqual(first.usage_ledger.snapshot()["prompt_tokens"], 10)
        self.assertEqual(second.usage_ledger.snapshot()["prompt_tokens"], 20)
        self.assertEqual(list(first.usage_ledger.snapshot()["by_stage"]), ["first"])
        self.assertEqual(list(second.usage_ledger.snapshot()["by_stage"]), ["second"])

    async def test_embed_preserves_order_and_records_usage(self):
        async def create(model, input):
            return response(prompt=len(input), completion=0,
                            vectors=[[float(int(text))] for text in input])

        context = QueryContext(llm_client=FakeClient(embed_create=create))
        with self.patches(embed_model=mock.Mock(return_value="embed-fixture")):
            vectors = await llm.embed_async(["1", "2", "3"], context=context, batch=2,
                                            stage="embed-query")
        self.assertEqual(vectors, [[1.0], [2.0], [3.0]])
        snapshot = context.usage_ledger.snapshot()
        self.assertEqual(snapshot["embed_calls"], 2)
        self.assertEqual(snapshot["embed_tokens"], 3)
        self.assertEqual(len(snapshot["call_events"]), 2)

    async def test_one_shared_async_client_is_reused_and_closed(self):
        built = FakeClient()
        with mock.patch.object(llm, "_async_client", None), \
             mock.patch.object(llm, "_build_async", return_value=built) as build:
            self.assertIs(llm.async_client(), built)
            self.assertIs(llm.async_client(), built)
            build.assert_called_once_with()
            await llm.close_async_client()
            self.assertTrue(built.closed)
            self.assertIsNone(llm._async_client)



class QueryContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_and_cancellation_are_owned_per_query(self):
        first, second = QueryContext(), QueryContext()
        await asyncio.gather(first.emit("status", value="A"), second.emit("status", value="B"))
        self.assertEqual((await first.progress.get())["value"], "A")
        self.assertEqual((await second.progress.get())["value"], "B")
        first.cancel()
        with self.assertRaises(runtime.QueryCancelled):
            first.check()
        second.check()

    async def test_already_cancelled_context_closes_eager_coroutine(self):
        async def work():
            await asyncio.sleep(10)

        context = QueryContext()
        context.cancel()
        coroutine = work()
        try:
            with self.assertRaisesRegex(runtime.QueryCancelled, "query cancelled"):
                await context.wait(coroutine)
            self.assertIsNone(coroutine.cr_frame, "cancelled wait left a coroutine unclosed")
        finally:
            if coroutine.cr_frame is not None:
                coroutine.close()




class _PatchStack:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()


if __name__ == "__main__":
    unittest.main()
