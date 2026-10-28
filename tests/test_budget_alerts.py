import asyncio
from datetime import date

import httpx

from app.db import get_sessionmaker
from app.services.outbox import check_budget, process_outbox_batch
from tests.conftest import auth_headers, execute, make_category, make_expense, scalar


async def run_worker_once() -> int:
    async with get_sessionmaker()() as session:
        return await process_outbox_batch(session)


async def test_expense_writes_outbox_event_in_same_transaction(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    budgeted = await make_category(client, h, "Food", budget="100.00")
    plain = await make_category(client, h, "Misc")
    await make_expense(client, h, budgeted, "10.00")
    await make_expense(client, h, plain, "10.00")  # no budget -> no event needed
    assert await scalar("SELECT count(*) FROM outbox_events") == 1
    assert await scalar("SELECT count(*) FROM notifications") == 0  # nothing sent inside the request


async def test_worker_creates_80_percent_alert(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h, "Food", budget="100.00")
    await make_expense(client, h, cat, "79.99", spent_on="2026-10-01")
    await run_worker_once()
    assert (await client.get("/notifications", headers=h)).json() == []

    await make_expense(client, h, cat, "0.01", spent_on="2026-10-02")  # exactly 80%
    assert await run_worker_once() == 1
    notes = (await client.get("/notifications", headers=h)).json()
    assert len(notes) == 1
    assert notes[0]["level"] == 80
    assert notes[0]["period"] == "2026-10-01"
    assert notes[0]["spent"] == "80.00"
    assert "passed 80%" in notes[0]["message"] and "(80.00%)" in notes[0]["message"]
    assert await scalar("SELECT count(*) FROM outbox_events WHERE processed_at IS NULL") == 0


async def test_crossing_100_percent_gives_both_levels_once(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h, "Food", budget="100.00")
    await make_expense(client, h, cat, "150.00")
    await make_expense(client, h, cat, "1.00")
    await run_worker_once()
    await make_expense(client, h, cat, "1.00")
    await run_worker_once()
    levels = sorted(n["level"] for n in (await client.get("/notifications", headers=h)).json())
    assert levels == [80, 100]


async def test_event_processed_twice_creates_one_notification(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h, "Food", budget="100.00")
    await make_expense(client, h, cat, "85.00")
    await run_worker_once()
    # Simulate a crash after the notification was written but before the event was marked done:
    await execute("UPDATE outbox_events SET processed_at = NULL")
    await run_worker_once()
    # ...and the handler being called directly again
    async with get_sessionmaker()() as session:
        user_id = await scalar("SELECT id FROM users")
        assert await check_budget(session, user_id, cat, date(2026, 10, 1)) == 0
        await session.commit()
    assert await scalar("SELECT count(*) FROM notifications") == 1


async def test_two_workers_in_parallel_create_one_notification(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h, "Food", budget="100.00")
    for _ in range(5):
        await make_expense(client, h, cat, "20.00")  # 5 events for the same category-month
    handled = await asyncio.gather(*[run_worker_once() for _ in range(4)])
    assert sum(handled) == 5  # SKIP LOCKED: each event handled by exactly one worker
    assert await scalar("SELECT count(*) FROM notifications WHERE level = 80") == 1
    assert await scalar("SELECT count(*) FROM notifications WHERE level = 100") == 1


async def test_editing_into_another_month_checks_new_month(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h, "Food", budget="100.00")
    exp = await make_expense(client, h, cat, "10.00", spent_on="2026-09-10")
    await run_worker_once()
    r = await client.patch(
        f"/expenses/{exp['id']}", json={"amount": "95.00", "spent_on": "2026-10-03"}, headers={**h, "If-Match": '"1"'}
    )
    assert r.status_code == 200
    await run_worker_once()
    notes = (await client.get("/notifications", headers=h)).json()
    assert [(n["period"], n["level"]) for n in notes] == [("2026-10-01", 80)]


async def test_notifications_are_private_and_paginated(client: httpx.AsyncClient) -> None:
    alice = await auth_headers(client, "alice@example.com")
    bob = await auth_headers(client, "bob@example.com")
    cat = await make_category(client, alice, "Food", budget="10.00")
    await make_expense(client, alice, cat, "20.00")
    await run_worker_once()
    assert (await client.get("/notifications", headers=bob)).json() == []
    page = (await client.get("/notifications?limit=1", headers=alice)).json()
    assert len(page) == 1
    older = (await client.get(f"/notifications?before_id={page[0]['id']}", headers=alice)).json()
    assert len(older) == 1 and older[0]["id"] < page[0]["id"]
