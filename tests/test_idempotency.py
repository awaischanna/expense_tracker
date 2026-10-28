import asyncio

import httpx

from tests.conftest import auth_headers, execute, make_category, scalar


def _body(cat: int, amount: str = "10.00") -> dict[str, object]:
    return {"category_id": cat, "amount": amount, "currency": "USD", "spent_on": "2026-10-05"}


async def test_same_key_same_body_replays_first_response(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    headers = {**h, "Idempotency-Key": "abc-123"}
    first = await client.post("/expenses", json=_body(cat), headers=headers)
    second = await client.post("/expenses", json=_body(cat), headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.headers["ETag"] == first.headers["ETag"]
    assert await scalar("SELECT count(*) FROM expenses") == 1


async def test_same_key_different_body_is_422(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    headers = {**h, "Idempotency-Key": "abc-123"}
    assert (await client.post("/expenses", json=_body(cat), headers=headers)).status_code == 201
    r = await client.post("/expenses", json=_body(cat, "99.00"), headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "idempotency_key_reused"
    assert await scalar("SELECT count(*) FROM expenses") == 1


async def test_parallel_requests_with_same_key_create_one_expense(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    headers = {**h, "Idempotency-Key": "race-key"}
    responses = await asyncio.gather(*[client.post("/expenses", json=_body(cat), headers=headers) for _ in range(5)])
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert await scalar("SELECT count(*) FROM expenses") == 1


async def test_keys_are_scoped_per_user(client: httpx.AsyncClient) -> None:
    alice = await auth_headers(client, "alice@example.com")
    bob = await auth_headers(client, "bob@example.com")
    a_cat = await make_category(client, alice)
    b_cat = await make_category(client, bob)
    r1 = await client.post("/expenses", json=_body(a_cat), headers={**alice, "Idempotency-Key": "k"})
    r2 = await client.post("/expenses", json=_body(b_cat), headers={**bob, "Idempotency-Key": "k"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


async def test_failed_request_does_not_burn_the_key(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    headers = {**h, "Idempotency-Key": "retry-me"}
    bad = await client.post("/expenses", json=_body(9999), headers=headers)  # unknown category
    assert bad.status_code == 404
    good = await client.post("/expenses", json=_body(cat), headers=headers)
    assert good.status_code == 201


async def test_expired_key_can_be_used_again(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    headers = {**h, "Idempotency-Key": "old-key"}
    await client.post("/expenses", json=_body(cat), headers=headers)
    await execute("UPDATE idempotency_keys SET expires_at = now() - interval '1 second'")
    r = await client.post("/expenses", json=_body(cat, "20.00"), headers=headers)
    assert r.status_code == 201
    assert "Idempotency-Replayed" not in r.headers
    assert await scalar("SELECT count(*) FROM expenses") == 2


async def test_empty_key_is_400(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    r = await client.post("/expenses", json=_body(cat), headers={**h, "Idempotency-Key": "   "})
    assert r.status_code == 400
