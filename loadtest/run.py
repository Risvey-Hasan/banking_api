"""Section 11 load test.

  python -m loadtest.run --rate 1000 --duration 60 --accounts 200
  python -m loadtest.run --rate 1000 --duration 20 --accounts 1 --label hot

The generator is multi-process on purpose. One Python process running httpx
saturates a single core at roughly 200 requests/second, so an API that can do
more than that cannot be measured with one generator process. --processes
controls how many; each handles rate/processes and reports raw measurements
back to the parent, which merges them so the percentiles are true percentiles
over every request rather than an average of per-shard percentiles.
"""

import argparse
import asyncio
import json
import os
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import httpx
import redis.asyncio as aioredis

from app.config import settings

CANDIDATE_BASES = os.environ.get(
    "LOADTEST_BASES",
    "http://localhost:8000,http://localhost:8001,http://localhost:8002",
).split(",")

_DELAY_SQL = """
    SELECT count(*) AS n,
           percentile_cont(0.50) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (processed_at - accepted_at))) AS p50,
           percentile_cont(0.95) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (processed_at - accepted_at))) AS p95,
           percentile_cont(0.99) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (processed_at - accepted_at))) AS p99,
           max(EXTRACT(EPOCH FROM (processed_at - accepted_at))) AS worst
      FROM transactions
     WHERE accepted_at >= $1 AND processed_at IS NOT NULL
"""

_MISMATCH_SQL = """
    SELECT count(*) FROM (
        SELECT a.id
          FROM accounts a
          LEFT JOIN transactions t
                 ON t.account_id = a.id
                AND t.status IN ('COMPLETED', 'REVERSED')
         GROUP BY a.id, a.balance
        HAVING a.balance <> COALESCE(SUM(
                   CASE WHEN t.type = 'CREDIT' THEN t.amount ELSE -t.amount END
               ), 0)
    ) AS drift
"""


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, int(round(p / 100.0 * (len(ordered) - 1))))
    return ordered[k]


# --------------------------------------------------------------------------
# Child process: submits one shard of the workload.
# --------------------------------------------------------------------------


async def _sender(clients, queue, latencies, codes) -> None:
    count = len(clients)
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        index, body = item
        started = time.perf_counter()
        try:
            response = await clients[index % count].post("/transactions", json=body)
            codes[response.status_code] += 1
        except Exception as exc:
            codes[type(exc).__name__] += 1
        latencies.append((time.perf_counter() - started) * 1000.0)
        queue.task_done()


async def _produce(queue, bodies, rate: float, lag: list[float]) -> None:
    """Enqueue at the target rate in 100 ms ticks, recording schedule lag."""
    tick = 0.1
    per_tick = max(1, int(rate * tick))
    start = time.perf_counter()
    index = 0
    ticks = 0
    while index < len(bodies):
        for _ in range(per_tick):
            if index >= len(bodies):
                break
            await queue.put((index, bodies[index]))
            index += 1
        ticks += 1
        due = start + ticks * tick
        behind = time.perf_counter() - due
        lag.append(max(0.0, behind))
        if behind < 0:
            await asyncio.sleep(-behind)


async def _submit(bases, bodies, rate: float, concurrency: int) -> dict:
    clients = [
        httpx.AsyncClient(
            base_url=base, timeout=60.0,
            limits=httpx.Limits(max_connections=concurrency,
                                max_keepalive_connections=concurrency),
        )
        for base in bases
    ]
    latencies: list[float] = []
    codes: Counter = Counter()
    lag: list[float] = []

    queue: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 2)
    senders = [
        asyncio.create_task(_sender(clients, queue, latencies, codes))
        for _ in range(concurrency)
    ]

    started = time.perf_counter()
    try:
        await _produce(queue, bodies, rate, lag)
        await queue.join()
        for _ in senders:
            await queue.put(None)
        await asyncio.gather(*senders)
    finally:
        await asyncio.gather(*(c.aclose() for c in clients))

    return {
        "latencies": latencies,
        "codes": {str(k): v for k, v in codes.items()},
        "lag_max": max(lag) if lag else 0.0,
        "seconds": time.perf_counter() - started,
    }


def run_shard(shard) -> dict:
    """Entry point for each child process. Must be importable at module level."""
    bases, bodies, rate, concurrency = shard
    return asyncio.run(_submit(bases, bodies, rate, concurrency))


# --------------------------------------------------------------------------
# Parent process: setup, backlog sampling, aggregation, reporting.
# --------------------------------------------------------------------------


def _sample_backlog(samples: list[dict], stop: threading.Event) -> None:
    """Runs in a thread with its own event loop, so it keeps sampling while
    the parent blocks on the process pool."""

    async def go() -> None:
        conn = await asyncpg.connect(dsn=settings.database_url)
        redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            while not stop.is_set():
                try:
                    pending = await conn.fetchval(
                        "SELECT count(*) FROM transactions WHERE status = 'PENDING'"
                    )
                    info = await redis.xpending(
                        settings.redis_stream, settings.redis_group
                    )
                    pel = info["pending"] if isinstance(info, dict) else 0
                    samples.append({"pending": pending, "pel": pel})
                except Exception:
                    pass
                await asyncio.sleep(1.0)
        finally:
            await redis.aclose()
            await conn.close()

    asyncio.run(go())


async def live_bases() -> list[str]:
    alive = []
    for base in CANDIDATE_BASES:
        try:
            async with httpx.AsyncClient(base_url=base, timeout=3.0) as c:
                if (await c.get("/health")).status_code == 200:
                    alive.append(base)
        except Exception:
            pass
    if not alive:
        raise SystemExit(f"no API responded on {CANDIDATE_BASES}")
    return alive


async def create_accounts(base: str, count: int) -> list[int]:
    ids = []
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as c:
        for _ in range(count):
            r = await c.post(
                "/accounts", json={"currency": "SAR", "initial_balance": "0.00"}
            )
            ids.append(r.json()["id"])
    return ids


async def drain(conn, run_id: str, limit_seconds: float = 600.0) -> float:
    started = time.perf_counter()
    while True:
        outstanding = await conn.fetchval(
            "SELECT count(*) FROM transactions "
            "WHERE status = 'PENDING' AND transaction_id LIKE $1",
            f"LT-{run_id}-%",
        )
        if outstanding == 0 or time.perf_counter() - started > limit_seconds:
            return time.perf_counter() - started
        await asyncio.sleep(1.0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=1000)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--accounts", type=int, default=200)
    ap.add_argument("--processes", type=int, default=4)
    ap.add_argument("--concurrency", type=int, default=50,
                    help="senders per process")
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    total = args.rate * args.duration
    bases = await live_bases()
    label = args.label or ("hot" if args.accounts == 1 else "spread")

    print(f"profile={label} rate={args.rate}/s duration={args.duration}s "
          f"total={total} accounts={args.accounts} targets={len(bases)} "
          f"processes={args.processes} concurrency/process={args.concurrency}")

    print("creating accounts...")
    account_ids = await create_accounts(bases[0], args.accounts)

    run_id = uuid.uuid4().hex[:8].upper()
    bodies = [
        {
            "transaction_id": f"LT-{run_id}-{i:07d}",
            "device_id": "DEV-LOAD",
            "account_id": account_ids[i % len(account_ids)],
            "type": "CREDIT",
            "amount": "1.00",
            "currency": "SAR",
            "timestamp": "2026-09-25T10:31:44Z",
        }
        for i in range(total)
    ]

    shards = [
        (bases, bodies[p::args.processes],
         args.rate / args.processes, args.concurrency)
        for p in range(args.processes)
    ]

    samples: list[dict] = []
    stop = threading.Event()
    sampler = threading.Thread(
        target=_sample_backlog, args=(samples, stop), daemon=True
    )
    sampler.start()

    started_at = datetime.now(timezone.utc)
    wall = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.processes) as pool:
        shard_results = list(pool.map(run_shard, shards))
    submit_seconds = time.perf_counter() - wall

    latencies: list[float] = []
    codes: Counter = Counter()
    lag_max = 0.0
    for result in shard_results:
        latencies.extend(result["latencies"])
        codes.update(result["codes"])
        lag_max = max(lag_max, result["lag_max"])

    print(f"submitted {total} in {submit_seconds:.1f}s; draining...")

    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        drain_seconds = await drain(conn, run_id)
        stop.set()
        sampler.join(timeout=5.0)

        delay = await conn.fetchrow(_DELAY_SQL, started_at)
        mismatches = await conn.fetchval(_MISMATCH_SQL)
    finally:
        await conn.close()

    errors = sum(v for k, v in codes.items() if not k.isdigit() or int(k) >= 400)

    report = {
        "profile": label,
        "target_rate": args.rate,
        "duration_s": args.duration,
        "accounts": args.accounts,
        "api_targets": len(bases),
        "generator_processes": args.processes,
        "concurrency_per_process": args.concurrency,
        "requests": total,
        "submit_seconds": round(submit_seconds, 2),
        "achieved_rps": round(total / submit_seconds, 1),
        "status_codes": dict(codes),
        "error_rate": round(errors / total, 5),
        "accept_latency_ms": {
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "p99": round(percentile(latencies, 99), 1),
            "max": round(max(latencies), 1) if latencies else 0,
        },
        "producer_schedule_lag_s": {"max": round(lag_max, 2)},
        "processing_delay_s": {
            "n": delay["n"],
            "p50": round(float(delay["p50"] or 0), 3),
            "p95": round(float(delay["p95"] or 0), 3),
            "p99": round(float(delay["p99"] or 0), 3),
            "max": round(float(delay["worst"] or 0), 3),
        },
        "backlog_peak": max((s["pending"] for s in samples), default=0),
        "pel_peak": max((s["pel"] for s in samples), default=0),
        "drain_seconds_after_submit": round(drain_seconds, 1),
        "balance_mismatches": mismatches,
    }

    out = Path("loadtest/results")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{label}-{run_id}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + json.dumps(report, indent=2))
    print(f"\nwritten to {path}")
    if mismatches:
        print("!! LEDGER MISMATCH - correctness broke under load")


if __name__ == "__main__":
    asyncio.run(main())
