from decimal import Decimal

import aiofiles
import aiofiles.os
import httpx

from app.db import get_sessionmaker
from app.models import ImportJob
from app.services.imports import RowError, parse_row, process_job, process_next_import, requeue_stale_jobs
from tests.conftest import auth_headers, execute, make_category, scalar

GOOD_AND_BAD = (
    "date,amount,currency,category,description\n"
    "2026-10-01,10.00,USD,Food,lunch\n"  # line 2 ok
    "2026-10-02,5.5,eur,food,coffee\n"  # line 3 ok (case-insensitive)
    "2026-13-01,1.00,USD,Food,bad date\n"  # line 4
    "2026-10-03,-4,USD,Food,negative\n"  # line 5
    "2026-10-03,1.999,USD,Food,3 decimals\n"  # line 6
    "2026-10-03,abc,USD,Food,not a number\n"  # line 7
    "2026-10-03,1.00,GBP,Food,bad currency\n"  # line 8
    "2026-10-03,1.00,USD,Unknown,bad category\n"  # line 9
    '2026-10-04,20.00,AED,Travel,"taxi, airport"\n'  # line 10 ok (quoted comma)
)


async def upload(client: httpx.AsyncClient, h: dict[str, str], content: str, name: str = "x.csv") -> httpx.Response:
    return await client.post("/expenses/import", files={"file": (name, content.encode(), "text/csv")}, headers=h)


async def run_import() -> bool:
    async with get_sessionmaker()() as session:
        return await process_next_import(session)


async def test_csv_import_counts_good_and_bad_rows(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    await make_category(client, h, "Food")
    await make_category(client, h, "Travel", budget="10.00")
    r = await upload(client, h, GOOD_AND_BAD)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    status = (await client.get(f"/imports/{job_id}", headers=h)).json()
    assert status["status"] == "pending"

    assert await run_import() is True
    status = (await client.get(f"/imports/{job_id}", headers=h)).json()
    assert status["status"] == "completed"
    assert (status["rows_total"], status["rows_done"], status["rows_failed"]) == (9, 3, 6)
    assert [e["row"] for e in status["errors"]] == [4, 5, 6, 7, 8, 9]
    reasons = " | ".join(e["reason"] for e in status["errors"])
    for word in ("invalid date", "greater than 0", "2 decimal places", "invalid amount", "currency", "category"):
        assert word in reasons
    assert await scalar("SELECT count(*) FROM expenses") == 3
    assert await scalar("SELECT description FROM expenses WHERE currency = 'AED'") == "taxi, airport"
    # Travel went over budget -> an outbox event was written with the batch
    assert await scalar("SELECT count(*) FROM outbox_events") == 1
    assert await run_import() is False  # nothing left


async def test_same_file_twice_does_not_duplicate(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    await make_category(client, h, "Food")
    await make_category(client, h, "Travel")
    first = (await upload(client, h, GOOD_AND_BAD)).json()
    await run_import()
    second = (await upload(client, h, GOOD_AND_BAD, name="renamed.csv")).json()
    assert second["job_id"] == first["job_id"]
    assert second["duplicate_of_previous_upload"] is True
    await run_import()
    assert await scalar("SELECT count(*) FROM expenses") == 3
    assert await scalar("SELECT count(*) FROM import_jobs") == 1


async def test_rerunning_a_crashed_job_does_not_duplicate_rows(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    await make_category(client, h, "Food")
    await make_category(client, h, "Travel")
    body = (await upload(client, h, GOOD_AND_BAD)).json()
    # Keep the file so the job can run twice, as if the worker died before finishing.
    async with get_sessionmaker()() as session:
        job = await session.get(ImportJob, body["job_id"])
        assert job is not None
        path = job.file_path
        async with aiofiles.open(path, "rb") as fh:
            content = await fh.read()
        await process_job(session, job)
    async with aiofiles.open(path, "wb") as fh:
        await fh.write(content)
    await execute("UPDATE import_jobs SET status='processing', heartbeat_at = now() - interval '1 hour'")
    async with get_sessionmaker()() as session:
        assert await requeue_stale_jobs(session) == 1
    assert await run_import() is True
    assert await scalar("SELECT count(*) FROM expenses") == 3
    status = (await client.get(f"/imports/{body['job_id']}", headers=h)).json()
    assert status["rows_done"] == 3 and status["rows_failed"] == 6


async def test_missing_columns_fails_job(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    body = (await upload(client, h, "when,how_much\n2026-10-01,5\n")).json()
    await run_import()
    status = (await client.get(f"/imports/{body['job_id']}", headers=h)).json()
    assert status["status"] == "failed"
    assert "missing columns" in status["errors"][0]["reason"]


async def test_large_import_in_batches(client: httpx.AsyncClient, monkeypatch: object) -> None:
    from app.config import get_settings

    h = await auth_headers(client)
    await make_category(client, h, "Food")
    rows = "".join(f"2026-10-{(i % 28) + 1:02d},{i + 1}.25,USD,Food,row {i}\n" for i in range(2500))
    body = (await upload(client, h, "date,amount,currency,category,description\n" + rows)).json()
    assert get_settings().import_batch_size == 1000  # -> 3 batches / transactions
    await run_import()
    status = (await client.get(f"/imports/{body['job_id']}", headers=h)).json()
    assert (status["rows_total"], status["rows_done"], status["rows_failed"]) == (2500, 2500, 0)
    total = await scalar("SELECT sum(amount) FROM expenses")
    assert total == sum(Decimal(f"{i + 1}.25") for i in range(2500))
    assert not await aiofiles.os.path.exists(f"{get_settings().import_dir}/{body['job_id']}.csv")  # temp file removed


async def test_import_status_of_other_user_is_404_and_empty_file_400(client: httpx.AsyncClient) -> None:
    alice = await auth_headers(client, "alice@example.com")
    bob = await auth_headers(client, "bob@example.com")
    body = (await upload(client, alice, "date,amount,currency,category\n")).json()
    assert (await client.get(f"/imports/{body['job_id']}", headers=bob)).status_code == 404
    r = await upload(client, alice, "")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_file"


def test_parse_row_unit() -> None:
    cats, cur = {"food": 1}, {"USD"}
    from datetime import date

    ok = parse_row(
        {"date": "2026-10-01", "amount": "1.5", "currency": "usd", "category": " Food "}, cats, cur, date(2020, 1, 1)
    )
    assert ok["amount"] == Decimal("1.5") and ok["currency"] == "USD" and ok["category_id"] == 1
    for bad in (
        {"date": "2019-01-01", "amount": "1", "currency": "USD", "category": "food"},
        {"date": "2026-10-01", "amount": "1e13", "currency": "USD", "category": "food"},
        {"date": "2026-10-01", "amount": "1", "currency": "USD", "category": "food", "description": "x" * 501},
    ):
        try:
            parse_row(bad, cats, cur, date(2020, 1, 1))
        except RowError:
            continue
        raise AssertionError(f"row should fail: {bad}")
