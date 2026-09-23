import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response, status

from app import db, redis_client

from app.api import accounts, transactions
from fastapi.exceptions import RequestValidationError
from app.errors import AppError, app_error_handler, validation_error_handler

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
app.include_router(accounts.router)
app.include_router(transactions.router)


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