from app import db, redis_client
from app.config import settings

_COUNTS = """
    SELECT kind::text AS kind, status::text AS status, count(*) AS n
      FROM transactions GROUP BY 1, 2
"""

_OLDEST_PENDING = """
    SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(accepted_at))), 0)
      FROM transactions WHERE status = 'PENDING'
"""

_MISMATCHES = """
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


def _metric(name: str, help_text: str, kind: str, samples) -> list[str]:
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        tags = (
            "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
            if labels else ""
        )
        out.append(f"{name}{tags} {value}")
    return out


async def render() -> str:
    lines: list[str] = []

    pg_ok = True
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(_COUNTS)
            oldest = await conn.fetchval(_OLDEST_PENDING)
            mismatches = await conn.fetchval(_MISMATCHES)
    except Exception:
        pg_ok, rows, oldest, mismatches = False, [], 0, -1

    lines += _metric(
        "txnsvc_transactions", "Transactions by kind and status", "gauge",
        [({"kind": r["kind"], "status": r["status"]}, r["n"]) for r in rows],
    )
    lines += _metric(
        "txnsvc_oldest_pending_age_seconds",
        "Age of the oldest PENDING transaction", "gauge",
        [({}, float(oldest))],
    )
    lines += _metric(
        "txnsvc_balance_mismatches",
        "Accounts whose stored balance disagrees with their ledger", "gauge",
        [({}, mismatches)],
    )

    redis_ok = True
    stream_len = pel = -1
    try:
        client = redis_client.client()
        stream_len = await client.xlen(settings.redis_stream)
        info = await client.xpending(settings.redis_stream, settings.redis_group)
        pel = info["pending"] if isinstance(info, dict) else 0
    except Exception:
        redis_ok = False

    lines += _metric("txnsvc_stream_length",
                     "Entries in the Redis stream", "gauge", [({}, stream_len)])
    lines += _metric("txnsvc_stream_pending_entries",
                     "Delivered but unacknowledged entries", "gauge", [({}, pel)])
    lines += _metric("txnsvc_up", "Component reachability", "gauge",
                     [({"component": "postgres"}, int(pg_ok)),
                      ({"component": "redis"}, int(redis_ok))])

    return "\n".join(lines) + "\n"