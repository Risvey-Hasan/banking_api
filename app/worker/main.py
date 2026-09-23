import asyncio
import logging
import os
import signal
import socket

from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app import db, redis_client
from app.config import settings
from app.worker.apply import apply_transaction

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")

CONSUMER = os.environ.get("WORKER_NAME") or socket.gethostname()
_stopping = asyncio.Event()


async def _ensure_group() -> None:
    try:
        await redis_client.client().xgroup_create(
            settings.redis_stream, settings.redis_group, id="0", mkstream=True
        )
        log.info("consumer group %s created", settings.redis_group)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
        log.info("consumer group %s already exists", settings.redis_group)


async def _handle(entry_id: str, fields: dict) -> None:
    transaction_id = fields.get("transaction_id")
    if not transaction_id:
        log.warning("entry %s carries no transaction_id; acking", entry_id)
    else:
        async with db.pool().acquire() as conn:
            outcome = await apply_transaction(conn, transaction_id)

        if outcome is None:
            log.info("tx=%s no-op (already handled)", transaction_id)
        else:
            log.info("tx=%s -> %s", transaction_id, outcome)
            if settings.crash_after_commit:
                log.error("CRASH_AFTER_COMMIT: exiting before ack, tx=%s", transaction_id)
                os._exit(1)

    await redis_client.client().xack(
        settings.redis_stream, settings.redis_group, entry_id
    )


async def _loop() -> None:
    client = redis_client.client()

    while not _stopping.is_set():
        try:
            _, claimed, _ = await client.xautoclaim(
                settings.redis_stream,
                settings.redis_group,
                CONSUMER,
                min_idle_time=settings.worker_min_idle_ms,
                start_id="0-0",
                count=settings.worker_batch,
            )
            for entry_id, fields in claimed:
                await _handle(entry_id, fields)

            fresh = await client.xreadgroup(
                settings.redis_group,
                CONSUMER,
                {settings.redis_stream: ">"},
                count=settings.worker_batch,
                block=settings.worker_block_ms,
            )
            for _stream, entries in fresh or []:
                for entry_id, fields in entries:
                    await _handle(entry_id, fields)


        except asyncio.CancelledError:
            raise

        except RedisTimeoutError:
            continue

        except Exception:
            log.exception("worker loop error; backing off")
            await asyncio.sleep(1.0)


async def main() -> None:
    await db.connect()
    if db._pool is None:
        log.error("no database; exiting so the restart policy can retry")
        raise SystemExit(1)

    await redis_client.connect(
        socket_timeout=settings.worker_block_ms / 1000 + 5.0
    )
    await _ensure_group()
    log.info("worker %s started", CONSUMER)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stopping.set)

    try:
        await _loop()
    finally:
        await redis_client.disconnect()
        await db.disconnect()
        log.info("worker %s stopped", CONSUMER)


if __name__ == "__main__":
    asyncio.run(main())