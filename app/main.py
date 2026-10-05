import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse

from app import db, redis_client, metrics
from app.api import accounts, auth, transactions, transfers
from app.errors import (
    AppError,
    app_error_handler,
    database_unavailable_handler,
    validation_error_handler,
)

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await redis_client.connect()
    yield
    await redis_client.disconnect()
    await db.disconnect()


app = FastAPI(
    title="Reliable Transaction Processing Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(db.DatabaseUnavailable, database_unavailable_handler)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(transfers.router)


# One self-contained page, served same-origin by the API itself. Same origin is
# the point rather than a shortcut: it means no CORS configuration, and it means
# the session cookie can be SameSite=Strict, which blocks cross-site requests
# before the CSRF token is even consulted.
_INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_INDEX, media_type="text/html")


@app.get("/health")
async def health(response: Response) -> dict[str, str]:
    pg_ok = await db.ping()
    redis_ok = await redis_client.ping()

    if not pg_ok:
        state = "unhealthy"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not redis_ok:
        state = "degraded"
    else:
        state = "healthy"

    return {
        "status": state,
        "postgres": "up" if pg_ok else "down",
        "redis": "up" if redis_ok else "down",
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> PlainTextResponse:
    return PlainTextResponse(
        await metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )