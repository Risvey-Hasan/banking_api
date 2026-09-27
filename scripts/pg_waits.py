"""Where is PostgreSQL's time actually going under load?

Run this WHILE a load test is in flight. Three very different bottlenecks look
identical from outside the database -- throughput stops climbing -- and each
one has a different fix, so guessing is expensive:

    CPU-bound     backends are state='active' with wait_event IS NULL.
                  Fix: less work per request, or more cores.
    fsync-bound   backends wait on WALSync/WALWrite and wal_sync_time is a
                  large share of wall-clock. Fix: group commit (commit_delay),
                  which shares one flush between concurrent committers without
                  weakening durability.
    lock-bound    backends wait on Lock (row contention) or LWLock (internal).
                  Fix: reduce contention on the hot row, not the hardware.

Method: sample pg_stat_activity every 50 ms and bucket what each backend is
doing, then read the cumulative pg_stat_wal / pg_stat_database counters before
and after and report the deltas. Sampling estimates where time is spent -- it
is cheap (one query per tick) but it is a sample, so treat small percentages
as noise.

For wal_sync_time to be populated, postgres must run with
track_wal_io_timing=on:

    PG_TRACK_WAL_IO=on docker compose up -d postgres

Usage, from the repo root, with a load test running in another terminal:

    python -m scripts.pg_waits --seconds 60
"""

import argparse
import asyncio
import time
from collections import Counter

import asyncpg

from app.config import settings

# Wait events that mean "blocked on the write-ahead log reaching the disk".
_WAL_SYNC_EVENTS = {"WALSync", "WALWrite", "WALFlush", "WALInitSync", "WALInitWrite"}


async def _snapshot(conn) -> dict:
    """Cumulative counters. Meaningless alone; the delta between two is not."""
    wal = await conn.fetchrow("SELECT * FROM pg_stat_wal")
    commits = await conn.fetchval(
        "SELECT xact_commit FROM pg_stat_database WHERE datname = current_database()"
    )
    snap = {"xact_commit": commits, "t": time.monotonic()}
    for key in ("wal_records", "wal_bytes", "wal_fpi", "wal_buffers_full",
                "wal_write", "wal_sync", "wal_write_time", "wal_sync_time"):
        try:
            snap[key] = wal[key]
        except (KeyError, IndexError):
            snap[key] = None          # column moved to pg_stat_io in PG 18+
    return snap


def _delta(before: dict, after: dict, key: str):
    if before.get(key) is None or after.get(key) is None:
        return None
    return after[key] - before[key]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--interval-ms", type=int, default=50)
    args = ap.parse_args()

    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        timing = await conn.fetchval("SHOW track_wal_io_timing")
        if timing != "on":
            print("NOTE  track_wal_io_timing is off -- wal_sync_time will read 0.")
            print("      Restart postgres with PG_TRACK_WAL_IO=on to get it.\n")

        before = await _snapshot(conn)

        # Sample what every other client backend in this database is doing.
        samples: Counter = Counter()
        ticks = 0
        deadline = time.monotonic() + args.seconds
        interval = args.interval_ms / 1000.0
        while time.monotonic() < deadline:
            rows = await conn.fetch(
                """
                SELECT state, wait_event_type, wait_event
                  FROM pg_stat_activity
                 WHERE datname = current_database()
                   AND pid <> pg_backend_pid()
                   AND backend_type = 'client backend'
                """
            )
            for r in rows:
                if r["state"] != "active":
                    samples[("idle", r["state"] or "unknown", "")] += 1
                elif r["wait_event_type"] is None:
                    samples[("running", "CPU / no wait", "")] += 1
                else:
                    samples[("waiting", r["wait_event_type"], r["wait_event"] or "")] += 1
            ticks += 1
            await asyncio.sleep(interval)

        after = await _snapshot(conn)
        elapsed = after["t"] - before["t"]

        busy = {k: v for k, v in samples.items() if k[0] != "idle"}
        busy_total = sum(busy.values()) or 1

        print(f"=== backend activity: {ticks} samples over {elapsed:.1f}s ===")
        print(f"{'share':>7}  {'kind':<9} {'wait_event_type':<18} wait_event")
        for (kind, etype, event), n in sorted(busy.items(), key=lambda kv: -kv[1])[:14]:
            print(f"{n / busy_total:>6.1%}  {kind:<9} {etype:<18} {event}")

        idle = sum(v for k, v in samples.items() if k[0] == "idle")
        print(f"\nactive backends/sample: {busy_total / ticks:.1f}")
        print(f"idle  backends/sample: {idle / ticks:.1f}"
              "   (pooled connections holding no work)")

        commits = _delta(before, after, "xact_commit") or 0
        print("\n=== commits ===")
        print(f"commits           : {commits}")
        print(f"commits/sec       : {commits / elapsed:.1f}")

        syncs = _delta(before, after, "wal_sync")
        sync_ms = _delta(before, after, "wal_sync_time")
        wal_bytes = _delta(before, after, "wal_bytes")
        print("\n=== write-ahead log ===")
        if syncs is not None:
            print(f"wal_sync calls    : {syncs}")
            if commits and syncs:
                print(f"commits per sync  : {commits / syncs:.2f}"
                      "   (>1 means commits already share flushes)")
        if sync_ms:
            print(f"wal_sync_time     : {sync_ms:.0f} ms"
                  f"  = {sync_ms / 1000 / elapsed:.1%} of wall-clock")
            if syncs:
                print(f"mean fsync        : {sync_ms / syncs:.3f} ms")
                print(f"implied fsync ceiling: {1000 / (sync_ms / syncs):.0f}/s"
                      "   (upper bound if every commit flushed alone)")
        if wal_bytes and commits:
            print(f"WAL per commit    : {wal_bytes / commits:.0f} bytes")

        # A reading, not a verdict. The numbers above are the evidence.
        wal_wait = sum(v for k, v in busy.items()
                       if k[2] in _WAL_SYNC_EVENTS or k[1] in _WAL_SYNC_EVENTS)
        cpu = sum(v for k, v in busy.items() if k[1] == "CPU / no wait")
        locks = sum(v for k, v in busy.items() if k[1] in ("Lock", "LWLock"))
        print("\n=== reading ===")
        print(f"on CPU            : {cpu / busy_total:.1%}")
        print(f"waiting on WAL    : {wal_wait / busy_total:.1%}")
        print(f"waiting on locks  : {locks / busy_total:.1%}")
        top = max(("CPU", cpu), ("WAL fsync", wal_wait), ("locks", locks),
                  key=lambda kv: kv[1])[0]
        print(f"dominant cost     : {top}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
