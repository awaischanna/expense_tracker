import httpx

from app.worker.main import housekeeping, run_once
from tests.conftest import auth_headers, execute, make_category, make_expense, scalar


async def test_run_once_processes_outbox_and_imports(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h, "Food", budget="10.00")
    await make_expense(client, h, cat, "50.00")
    await client.post(
        "/expenses/import",
        files={"file": ("a.csv", b"date,amount,currency,category\n2026-10-01,1.00,USD,Food\n", "text/csv")},
        headers=h,
    )
    assert await run_once() is True
    assert await scalar("SELECT count(*) FROM notifications") == 2
    assert await scalar("SELECT status FROM import_jobs") == "completed"
    await run_once()  # processes the event written by the import batch
    assert await run_once() is False  # idle


async def test_housekeeping_purges_expired_keys_and_old_events(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h, "Food", budget="100.00")
    await client.post(
        "/expenses",
        json={"category_id": cat, "amount": "1.00", "currency": "USD", "spent_on": "2026-10-01"},
        headers={**h, "Idempotency-Key": "k1"},
    )
    await run_once()
    await execute("UPDATE idempotency_keys SET expires_at = now() - interval '1 minute'")
    await execute("UPDATE outbox_events SET processed_at = now() - interval '8 days'")
    await housekeeping()
    assert await scalar("SELECT count(*) FROM idempotency_keys") == 0
    assert await scalar("SELECT count(*) FROM outbox_events") == 0


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("http://test/health")
    assert r.json() == {"status": "ok"}
    r = await client.get("/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
