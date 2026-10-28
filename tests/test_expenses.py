import asyncio

import httpx
import pytest

from tests.conftest import auth_headers, make_category, make_expense, scalar


async def test_create_expense_returns_201_decimal_strings_and_etag(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    r = await client.post(
        "/expenses",
        json={"category_id": cat, "amount": "12.50", "currency": "eur", "spent_on": "2026-10-05"},
        headers=h,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["amount"] == "12.50"  # Decimal serialised as string, never float
    assert body["currency"] == "EUR"
    assert body["version"] == 1
    assert r.headers["ETag"] == '"1"'
    assert r.headers["Location"] == f"/api/v1/expenses/{body['id']}"


@pytest.mark.parametrize(
    "amount",
    ["0", "0.00", "-5", "-0.01", "1.234", "10.001", "abc", "NaN", "Infinity", "1e20"],
)
async def test_invalid_amounts_are_rejected(client: httpx.AsyncClient, amount: str) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    r = await client.post(
        "/expenses",
        json={"category_id": cat, "amount": amount, "currency": "USD", "spent_on": "2026-10-05"},
        headers=h,
    )
    assert r.status_code == 422, (amount, r.text)
    assert r.json()["error"]["code"] == "validation_error"
    assert await scalar("SELECT count(*) FROM expenses") == 0


async def test_unknown_fields_and_currency_rejected(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    base = {"category_id": cat, "amount": "1.00", "spent_on": "2026-10-05"}
    assert (await client.post("/expenses", json={**base, "currency": "GBP"}, headers=h)).status_code == 422
    r = await client.post("/expenses", json={**base, "currency": "USD", "user_id": 99}, headers=h)
    assert r.status_code == 422


async def test_date_without_exchange_rate_is_rejected(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    r = await client.post(
        "/expenses",
        json={"category_id": cat, "amount": "1.00", "currency": "USD", "spent_on": "2019-12-31"},
        headers=h,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_exchange_rate"


async def test_other_users_data_returns_404_not_403(client: httpx.AsyncClient) -> None:
    alice = await auth_headers(client, "alice@example.com")
    bob = await auth_headers(client, "bob@example.com")
    alice_cat = await make_category(client, alice)
    exp = await make_expense(client, alice, alice_cat)

    assert (await client.get(f"/expenses/{exp['id']}", headers=bob)).status_code == 404
    r = await client.patch(f"/expenses/{exp['id']}", json={"amount": "1.00"}, headers={**bob, "If-Match": '"1"'})
    assert r.status_code == 404
    assert (await client.delete(f"/expenses/{exp['id']}", headers=bob)).status_code == 404
    # Bob cannot file an expense under Alice's category either
    r = await client.post(
        "/expenses",
        json={"category_id": alice_cat, "amount": "1.00", "currency": "USD", "spent_on": "2026-10-05"},
        headers=bob,
    )
    assert r.status_code == 404
    assert (await client.get("/expenses", headers=bob)).json()["items"] == []
    # Alice still sees hers, untouched
    r = await client.get(f"/expenses/{exp['id']}", headers=alice)
    assert r.status_code == 200 and r.json()["amount"] == "10.00"


async def test_list_filters(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    food = await make_category(client, h, "Food")
    travel = await make_category(client, h, "Travel")
    await make_expense(client, h, food, "5.00", "USD", "2026-10-01", "Coffee 50% off")
    await make_expense(client, h, food, "25.00", "EUR", "2026-10-10", "Dinner")
    await make_expense(client, h, travel, "300.00", "AED", "2026-10-20", "Taxi to airport")

    async def ids(**params: str) -> list[str]:
        r = await client.get("/expenses", params=params, headers=h)
        assert r.status_code == 200, r.text
        return [e["description"] for e in r.json()["items"]]

    assert await ids() == ["Taxi to airport", "Dinner", "Coffee 50% off"]
    assert await ids(category_id=str(food)) == ["Dinner", "Coffee 50% off"]
    assert await ids(date_from="2026-10-05", date_to="2026-10-10") == ["Dinner"]
    assert await ids(min_amount="10", max_amount="100") == ["Dinner"]
    assert await ids(currency="aed") == ["Taxi to airport"]
    assert await ids(q="AIRPORT") == ["Taxi to airport"]
    assert await ids(q="50%") == ["Coffee 50% off"]  # % is matched literally, not as a wildcard
    assert await ids(q="%") == ["Coffee 50% off"]
    r = await client.get("/expenses", params={"date_from": "2026-10-10", "date_to": "2026-10-01"}, headers=h)
    assert r.status_code == 422


async def test_cursor_pagination_has_no_duplicates_or_gaps_while_rows_are_added(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    original = set()
    # many rows share the same date -> the id tie-breaker matters
    for i in range(25):
        e = await make_expense(client, h, cat, "1.00", spent_on=f"2026-10-{(i % 5) + 1:02d}")
        original.add(e["id"])

    seen: list[int] = []
    cursor = None
    page = 0
    while True:
        params = {"limit": "7"}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/expenses", params=params, headers=h)
        assert r.status_code == 200
        body = r.json()
        seen.extend(e["id"] for e in body["items"])
        page += 1
        if page == 1:
            # new rows arrive between page requests: newer, same-day and older ones
            for d in ("2026-10-28", "2026-10-03", "2026-10-01"):
                await make_expense(client, h, cat, "2.00", spent_on=d)
        cursor = body["next_cursor"]
        if not cursor:
            break

    assert len(seen) == len(set(seen)), "duplicate rows across pages"
    assert original <= set(seen), "an original row was skipped"
    # order is spent_on DESC, id DESC on every page
    rows = [(e["spent_on"], e["id"]) for e in (await client.get("/expenses?limit=100", headers=h)).json()["items"]]
    assert rows == sorted(rows, reverse=True)


async def test_invalid_cursor_is_400(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    r = await client.get("/expenses", params={"cursor": "!!garbage!!"}, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_cursor"


async def test_patch_with_if_match_updates_and_bumps_version(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    other = await make_category(client, h, "Other")
    exp = await make_expense(client, h, cat)
    etag = (await client.get(f"/expenses/{exp['id']}", headers=h)).headers["ETag"]
    r = await client.patch(
        f"/expenses/{exp['id']}",
        json={"amount": "99.99", "description": "edited", "category_id": other},
        headers={**h, "If-Match": etag},
    )
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == "99.99" and r.json()["version"] == 2
    assert r.headers["ETag"] == '"2"'


async def test_patch_without_if_match_is_428_and_stale_is_412(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    exp = await make_expense(client, h, cat)
    r = await client.patch(f"/expenses/{exp['id']}", json={"amount": "2.00"}, headers=h)
    assert r.status_code == 428
    assert (
        await client.patch(f"/expenses/{exp['id']}", json={"amount": "2.00"}, headers={**h, "If-Match": '"1"'})
    ).status_code == 200
    r = await client.patch(f"/expenses/{exp['id']}", json={"amount": "3.00"}, headers={**h, "If-Match": '"1"'})
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "version_conflict"
    assert r.headers["ETag"] == '"2"'
    r = await client.patch(f"/expenses/{exp['id']}", json={"amount": "3.00"}, headers={**h, "If-Match": "abc"})
    assert r.status_code == 400
    r = await client.patch(f"/expenses/{exp['id']}", json={}, headers={**h, "If-Match": '"2"'})
    assert r.status_code == 422
    r = await client.patch(f"/expenses/{exp['id']}", json={"amount": None}, headers={**h, "If-Match": '"2"'})
    assert r.status_code == 422


async def test_parallel_patches_with_same_etag_one_wins(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    exp = await make_expense(client, h, cat)
    headers = {**h, "If-Match": '"1"'}
    r1, r2 = await asyncio.gather(
        client.patch(f"/expenses/{exp['id']}", json={"amount": "111.00"}, headers=headers),
        client.patch(f"/expenses/{exp['id']}", json={"amount": "222.00"}, headers=headers),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 412]
    winner = r1 if r1.status_code == 200 else r2
    current = (await client.get(f"/expenses/{exp['id']}", headers=h)).json()
    assert current["amount"] == winner.json()["amount"]
    assert current["version"] == 2


async def test_delete_expense(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    exp = await make_expense(client, h, cat)
    assert (await client.delete(f"/expenses/{exp['id']}", headers=h)).status_code == 204
    assert (await client.get(f"/expenses/{exp['id']}", headers=h)).status_code == 404
    assert (await client.delete(f"/expenses/{exp['id']}", headers=h)).status_code == 404
