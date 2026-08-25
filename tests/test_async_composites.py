"""Stage 6: structured composite concurrency and shared query guardrails."""
import asyncio
import os
import sys
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ard_client
import harness
import llm
from query_context import ProviderPermits, QueryBudget, QueryContext
import runtime


def context(**kwargs):
    return QueryContext(usage_ledger=llm.Ledger(), discovery_ledger=ard_client.DiscoveryUsage(),
                        **kwargs)


class StructuredConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_group_executes_concurrently_but_consumes_plan_order(self):
        started = 0
        all_started = asyncio.Event()

        async def branch(index, delay, branch_context):
            nonlocal started
            started += 1
            if started == 3:
                all_started.set()
            await all_started.wait()
            await asyncio.sleep(delay)
            return index

        actual = await harness._ordered(context(), [
            lambda child, index=index, delay=delay: branch(index, delay, child)
            for index, delay in enumerate((.03, .02, .01))])
        self.assertEqual(actual, [0, 1, 2])

    async def test_cancelling_parent_cancels_every_child(self):
        active = 0
        finished = asyncio.Event()

        async def branch(child):
            nonlocal active
            active += 1
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
                if active == 0:
                    finished.set()

        task = asyncio.create_task(harness._ordered(context(), [branch, branch, branch]))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(active, 0)

    async def test_fanout_and_attempt_budgets_are_shared_without_await_races(self):
        ctx = context(budget=QueryBudget(max_attempts=2, max_fanout=2))
        ctx.budget.consume_attempt(); ctx.fork().budget.consume_attempt()
        with self.assertRaisesRegex(runtime.QueryBudgetExceeded, "160|attempt|limit"):
            ctx.budget.consume_attempt()
        ctx.budget.consume_fanout(2)
        with self.assertRaisesRegex(runtime.QueryBudgetExceeded, "fan-out"):
            ctx.fork().budget.consume_fanout()

    async def test_refusal_inside_task_group_remains_an_ordinary_refusal(self):
        async def refuses(_context):
            raise runtime.Refused("fixture refusal")
        with self.assertRaisesRegex(runtime.Refused, "fixture refusal"):
            await harness._ordered(context(), [refuses])

    async def test_task_group_surfaces_real_failure_not_exception_group(self):
        async def fails(_context):
            raise RuntimeError("accessor blew up")
        with self.assertRaisesRegex(RuntimeError, "accessor blew up") as raised:
            await harness._ordered(context(), [fails])
        self.assertNotIn("TaskGroup", str(raised.exception))

    async def test_task_group_preserves_refusal_subclasses(self):
        async def exhausted(_context):
            raise runtime.QueryBudgetExceeded("budget exhausted")
        with self.assertRaises(runtime.QueryBudgetExceeded):
            await harness._ordered(context(), [exhausted])

    async def test_refusal_outranks_sibling_cancellation(self):
        sibling_started = asyncio.Event()

        async def cancelled(_context):
            sibling_started.set()
            await asyncio.Event().wait()

        async def refuses(_context):
            await sibling_started.wait()
            raise runtime.Refused("real refusal")

        with self.assertRaisesRegex(runtime.Refused, "real refusal"):
            await harness._ordered(context(), [cancelled, refuses])

    async def test_real_failure_outranks_explicit_query_cancellation(self):
        both_started = asyncio.Event()
        started = 0

        async def rendezvous():
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()

        async def deadline(_context):
            await rendezvous()
            raise runtime.QueryCancelled("query deadline exceeded")

        async def fails(_context):
            await rendezvous()
            raise RuntimeError("accessor blew up")

        with self.assertRaisesRegex(RuntimeError, "accessor blew up"):
            await harness._ordered(context(), [deadline, fails])

    async def test_correlation_refuses_high_blowup_before_materializing(self):
        ctx = context()
        hits = [[{"identifier": "a", "title": "A"}],
                [{"identifier": "b", "title": "B"}]]
        caps = {"tract": {"grain": "tract", "rows_per_unit": {"county": 100}},
                "county": {"grain": "county"}}
        with mock.patch.object(llm, "chat_async", mock.AsyncMock(return_value=(
                '{"measure_a":"a","measure_b":"b","grain":"county","state_fips":"06"}'))), \
             mock.patch.object(ard_client, "search_async", mock.AsyncMock(side_effect=hits)), \
             mock.patch.object(harness.planner, "capabilities", return_value=caps), \
             mock.patch.object(harness, "_materialize_async", mock.AsyncMock()) as materialize:
            with self.assertRaisesRegex(runtime.Refused, "too expensive"):
                await harness._run_correlate_async("correlate", {}, context=ctx)
        materialize.assert_not_awaited()

    async def test_provider_permit_is_released_between_calls_and_during_backoff(self):
        permits = ProviderPermits({"publisher": 1})
        ctx = context(permits=permits)
        entered = asyncio.Event()

        async def call():
            entered.set()
            return "done"

        self.assertEqual(await ctx.provider_call("publisher", call), "done")
        self.assertEqual(permits.snapshot()["publisher"]["active"], 0)
        sleeping = asyncio.create_task(ctx.sleep(.02))
        await entered.wait()
        self.assertEqual(await ctx.provider_call("publisher", call), "done")
        await sleeping

    async def test_async_fanout_creates_no_worker_threads(self):
        before = {thread.ident for thread in threading.enumerate()}
        ctx = context()
        with mock.patch.object(harness, "retrieve_for", mock.AsyncMock(
                side_effect=[{"value": 1, "source": "s"}, {"value": 3, "source": "s"}])):
            result = await harness._run_fanout_async("compare", {
                "attribute": "value", "entities": ["A", "B"]}, "comparison", context=ctx)
        self.assertEqual(result["highest"], "B")
        self.assertEqual(before, {thread.ident for thread in threading.enumerate()})


if __name__ == "__main__":
    unittest.main()
