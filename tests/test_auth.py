import asyncio

import httpx

from tests.conftest import auth_headers, login, register, scalar


async def test_register_returns_201_without_password(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/auth/register", json={"email": "Ali@Example.com", "password": "s3cret-pass", "base_currency": "pkr"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "ali@example.com"
    assert body["base_currency"] == "PKR"
    assert "password" not in body and "password_hash" not in body
    stored = await scalar("SELECT password_hash FROM users")
    assert stored.startswith("$argon2")


async def test_register_duplicate_email_is_409(client: httpx.AsyncClient) -> None:
    await register(client, "ali@example.com")
    r = await client.post(
        "/auth/register", json={"email": "ALI@example.com", "password": "another-pass", "base_currency": "USD"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


async def test_register_validation_uses_common_error_format(client: httpx.AsyncClient) -> None:
    r = await client.post("/auth/register", json={"email": "bad", "password": "short", "base_currency": "XYZ"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    fields = {d["loc"][-1] for d in err["details"]}
    assert {"email", "password", "base_currency"} <= fields


async def test_login_wrong_password_is_401(client: httpx.AsyncClient) -> None:
    await register(client)
    r = await client.post("/auth/login", json={"email": "ali@example.com", "password": "wrong-pass"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"


async def test_sixth_failed_login_returns_429_with_retry_after(client: httpx.AsyncClient) -> None:
    await register(client)
    for _ in range(5):
        r = await client.post("/auth/login", json={"email": "ali@example.com", "password": "wrong-pass"})
        assert r.status_code == 401
    r = await client.post("/auth/login", json={"email": "ali@example.com", "password": "wrong-pass"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert 0 < int(r.headers["Retry-After"]) <= 15 * 60
    # even the right password is blocked now
    r = await client.post("/auth/login", json={"email": "ali@example.com", "password": "s3cret-pass"})
    assert r.status_code == 429


async def test_parallel_failed_logins_cannot_bypass_limit(client: httpx.AsyncClient) -> None:
    await register(client)
    results = await asyncio.gather(
        *[client.post("/auth/login", json={"email": "ali@example.com", "password": "nope-nope"}) for _ in range(10)]
    )
    codes = sorted(r.status_code for r in results)
    assert codes.count(401) == 5
    assert codes.count(429) == 5


async def test_successful_login_does_not_count_as_failure(client: httpx.AsyncClient) -> None:
    await register(client)
    for _ in range(4):
        await client.post("/auth/login", json={"email": "ali@example.com", "password": "wrong-pass"})
    for _ in range(3):
        await login(client)  # would hit 429 if successes were counted
    r = await client.post("/auth/login", json={"email": "ali@example.com", "password": "wrong-pass"})
    assert r.status_code == 401  # 5th failure still allowed


async def test_refresh_rotates_tokens(client: httpx.AsyncClient) -> None:
    await register(client)
    tokens = await login(client)
    r = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    me = await client.get("/categories", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert me.status_code == 200


async def test_reusing_refresh_token_revokes_all_tokens(client: httpx.AsyncClient) -> None:
    await register(client)
    first = await login(client)
    second = (await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})).json()

    # The OLD token is used again -> theft detected
    r = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "token_reuse_detected"

    # ...so the legit newest refresh token is dead too
    r = await client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 401
    # ...and access tokens issued before are rejected
    r = await client.get("/categories", headers={"Authorization": f"Bearer {second['access_token']}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "token_revoked"
    assert await scalar("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL") == 0


async def test_logout_revokes_refresh_token(client: httpx.AsyncClient) -> None:
    await register(client)
    tokens = await login(client)
    r = await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 204
    r = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"  # logout is not treated as theft


async def test_bad_tokens_return_401(client: httpx.AsyncClient) -> None:
    headers = await auth_headers(client)
    assert (await client.get("/expenses")).status_code == 401
    r = await client.get("/expenses", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"
    # an access token is not a refresh token
    access = headers["Authorization"].split()[1]
    r = await client.post("/auth/refresh", json={"refresh_token": access})
    assert r.status_code == 401
