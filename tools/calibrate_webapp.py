#!/usr/bin/env python3
"""Bounded closed-loop Web App calibration. Never runs without explicit traffic and cost caps."""
import argparse
import asyncio
import json
import statistics
import time

import httpx


async def run(args):
    with open(args.queries, encoding="utf-8") as stream:
        queries = [line.strip() for line in stream if line.strip() and not line.startswith("#")]
    if not queries:
        raise SystemExit("query file contains no queries")
    authorized_requests = min(args.max_requests,
                              int(args.max_cost_usd / args.max_cost_per_request_usd))
    if authorized_requests < 1:
        raise SystemExit("cost ceiling authorizes zero requests at the stated maximum per-request cost")
    limits = httpx.Limits(max_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), timeout=args.timeout,
                                 limits=limits) as client:
        latencies, statuses, lock = [], {}, asyncio.Lock()
        next_index = 0

        async def worker():
            nonlocal next_index
            while True:
                async with lock:
                    if next_index >= authorized_requests:
                        return
                    index = next_index
                    next_index += 1
                started = time.monotonic()
                try:
                    response = await client.post("/ask", json={
                        "query": queries[index % len(queries)], "streaming": False})
                    status = str(response.status_code)
                except Exception as exc:
                    status = type(exc).__name__
                latencies.append((time.monotonic() - started) * 1000)
                statuses[status] = statuses.get(status, 0) + 1
        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        health = (await client.get("/healthz")).json()
    ordered = sorted(latencies)
    percentile = lambda p: ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))]
    print(json.dumps({
        "requests": len(ordered), "concurrency": args.concurrency, "statuses": statuses,
        "latency_ms": {"mean": round(statistics.mean(ordered), 1),
                       "p50": round(percentile(.50), 1), "p95": round(percentile(.95), 1),
                       "p99": round(percentile(.99), 1)},
        "estimated_cost_ceiling_usd": round(
            authorized_requests * args.max_cost_per_request_usd, 6),
        "health": health,
    }, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--queries", required=True, help="one query per line")
    parser.add_argument("--concurrency", required=True, type=int)
    parser.add_argument("--max-requests", required=True, type=int)
    parser.add_argument("--max-cost-usd", required=True, type=float)
    parser.add_argument("--max-cost-per-request-usd", required=True, type=float,
                        help="conservative bound used to cap requests before traffic starts")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    if min(args.concurrency, args.max_requests) <= 0 or min(
            args.max_cost_usd, args.max_cost_per_request_usd) <= 0:
        parser.error("concurrency, request cap, and both cost values must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
