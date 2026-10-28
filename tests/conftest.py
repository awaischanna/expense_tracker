"""Test setup.

* Real PostgreSQL (database `expenses_test`, created fresh and migrated with Alembic once
  per run, so the migrations themselves are tested too).
* Real Redis (db 15).
* S3 = moto server in a thread (no real AWS calls).
* Every test starts clean: all tables TRUNCATEd, Redis db flushed, known exchange rates
  inserted.

Override connection details with TEST_DATABASE_URL / TEST_REDIS_URL.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import dotenv_values  # installed with pydantic-settings

# Use the same Postgres user/password as docker-compose (read from the local .env), so
# `pytest` works against `docker compose up -d postgres redis` with no extra setup.
_dotenv = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
_PG_USER = os.environ.get("POSTGRES_USER") or _dotenv.get("POSTGRES_USER") or "postgres"
_PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD") or _dotenv.get("POSTGRES_PASSWORD") or "postgres"
_PG_PORT = os.environ.get("POSTGRES_PORT") or _dotenv.get("POSTGRES_PORT") or "5432"
_PG_AUTH = f"{quote(_PG_USER, safe='')}:{quote(_PG_PASSWORD, safe='')}"
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", f"postgresql+asyncpg://{_PG_AUTH}@localhost:{_PG_PORT}/expenses_test")
MOTO_PORT = int(os.environ.get("TEST_MOTO_PORT", "5055"))

# Must be set before anything from `app` is imported.
os.environ.update(
    {
        "DATABASE_URL": TEST_DB_URL,
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
        "JWT_SECRET": "test-only-secret-not-used-anywhere-real-0123456789",
        "DB_NULL_POOL": "true",
        "S3_ENDPOINT_URL": f"http://127.0.0.1:{MOTO_PORT}",
        "S3_BUCKET": "test-receipts",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "IMPORT_DIR": tempfile.mkdtemp(prefix="imports-"),
        "WORKER_POLL_SECONDS": "0.05",
    }
)

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from moto.server import ThreadedMotoServer  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import storage  # noqa: E402
from app.db import get_sessionmaker  # noqa: E402
from app.main import app  # noqa: E402
from app.redis_client import get_redis  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Fixed test rates (rate_to_usd). Easy numbers so expected totals are obvious.
BASE_RATES = {"USD": Decimal("1"), "EUR": Decimal("1.2"), "AED": Decimal("0.25"), "PKR": Decimal("0.004")}
RATES_FROM = date(2020, 1, 1)

TABLES = (
    "s3_deletions, import_jobs, notifications, outbox_events, idempotency_keys, "
    "expenses, categories, refresh_tokens, users, exchange_rates"
)


async def _recreate_database() -> None:
    plain = TEST_DB_URL.replace("+asyncpg", "")
    base, db_name = plain.rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    asyncio.run(_recreate_database())
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True, env=os.environ.copy())
    yield


@pytest.fixture(scope="session", autouse=True)
def s3_server() -> Iterator[None]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=MOTO_PORT, verbose=False)
    server.start()
    yield
    server.stop()


@pytest.fixture(autouse=True)
async def clean_state(database: None, s3_server: None) -> AsyncIterator[None]:
    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await session.execute(
            text("INSERT INTO exchange_rates (currency, valid_on, rate_to_usd) VALUES (:c, :d, :r)"),
            [{"c": c, "d": RATES_FROM, "r": r} for c, r in BASE_RATES.items()],
        )
        await session.commit()
    await get_redis().flushdb()
    await storage.ensure_bucket()
    yield


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        yield c


# ---------------- helpers ----------------
async def register(client: httpx.AsyncClient, email: str = "ali@example.com", base: str = "USD") -> None:
    r = await client.post("/auth/register", json={"email": email, "password": "s3cret-pass", "base_currency": base})
    assert r.status_code == 201, r.text


async def login(client: httpx.AsyncClient, email: str = "ali@example.com") -> dict[str, Any]:
    r = await client.post("/auth/login", json={"email": email, "password": "s3cret-pass"})
    assert r.status_code == 200, r.text
    return r.json()


async def auth_headers(client: httpx.AsyncClient, email: str = "ali@example.com", base: str = "USD") -> dict[str, str]:
    await register(client, email, base)
    tokens = await login(client, email)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def make_category(
    client: httpx.AsyncClient, headers: dict[str, str], name: str = "Food", budget: str | None = None
) -> int:
    body: dict[str, Any] = {"name": name}
    if budget is not None:
        body["monthly_budget"] = budget
    r = await client.post("/categories", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def make_expense(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    category_id: int,
    amount: str = "10.00",
    currency: str = "USD",
    spent_on: str = "2026-10-05",
    description: str = "",
) -> dict[str, Any]:
    r = await client.post(
        "/expenses",
        json={
            "category_id": category_id,
            "amount": amount,
            "currency": currency,
            "spent_on": spent_on,
            "description": description,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def add_rate(currency: str, valid_on: date, rate: str) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            text("INSERT INTO exchange_rates (currency, valid_on, rate_to_usd) VALUES (:c, :d, :r)"),
            {"c": currency, "d": valid_on, "r": Decimal(rate)},
        )
        await session.commit()


async def scalar(sql: str, **params: Any) -> Any:
    async with get_sessionmaker()() as session:
        return (await session.execute(text(sql), params)).scalar()


async def execute(sql: str, **params: Any) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(text(sql), params)
        await session.commit()