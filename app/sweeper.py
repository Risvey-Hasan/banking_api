import asyncio
import logging
import signal

from app import db, redis_client
from app.config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sweeper")

_stopping = asyncio.Event()

_CLAIM_DUE = """
    UPDATE transactions
       SET next_attempt_at = now() + make_interval(secs => $2)
     WHERE transaction_id IN (
         SELECT transaction_id
           FROM transactions
          WHERE status = 'PENDING'
            AND next_attempt_at <= now()
          ORDER BY next_attempt_at
          LIMIT $1
          FOR UPDATE SKIP LOCKED
     )
    RETURNING transaction_id
"""


async def _sweep_once() -> int:
    client = redis_client.client()

    depth = await client.xlen(settings.redis_stream)
    if depth > settings.sweeper_max_stream_len:
        log.info("backpressure: stream depth %d, skipping sweep", depth)
        return 0

    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            _CLAIM_DUE,
            settings.sweeper_batch,
            settings.sweeper_redispatch_backoff_seconds,
        )

    for row in rows:
        await client.xadd(
            settings.redis_stream,
            {"transaction_id": row["transaction_id"]},
            maxlen=settings.redis_stream_maxlen,
            approximate=True,
        )

    if rows:
        log.info("re-dispatched %d transaction(s)", len(rows))
    return len(rows)


async def _loop() -> None:
    while not _stopping.is_set():
        try:
            dispatched = await _sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sweep failed; backing off")
            await asyncio.sleep(1.0)
            continue

        # Batch came back full: there is more waiting, so don't sleep.
        if dispatched < settings.sweeper_batch:
            await asyncio.sleep(settings.sweeper_interval_seconds)


async def main() -> None:
    await db.connect()
    if db._pool is None:
        log.error("no database; exiting so the restart policy can retry")
        raise SystemExit(1)

    await redis_client.connect()
    log.info("sweeper started (interval=%ss batch=%s)",
             settings.sweeper_interval_seconds, settings.sweeper_batch)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stopping.set)

    try:
        await _loop()
    finally:
        await redis_client.disconnect()
        await db.disconnect()
        log.info("sweeper stopped")


if __name__ == "__main__":
    asyncio.run(main())