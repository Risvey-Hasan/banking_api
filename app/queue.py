import logging

from app import redis_client
from app.config import settings

log = logging.getLogger(__name__)


async def nudge(transaction_id: str) -> bool:
    """Best-effort dispatch. Failure is acceptable — the sweeper re-dispatches."""
    try:
        await redis_client.client().xadd(
            settings.redis_stream,
            {"transaction_id": transaction_id},
            maxlen=settings.redis_stream_maxlen,
            approximate=True,
        )
        return True
    except Exception:
        log.warning(
            "nudge failed for %s; sweeper will re-dispatch",
            transaction_id,
            exc_info=True,
        )
        return False