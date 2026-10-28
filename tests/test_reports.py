from datetime import UTC, date, datetime

import httpx

from tests.conftest import add_rate, auth_headers, make_category, make_expense


async def test_monthly_report_mixed_currencies_and_missing_rate_days(client: httpx.AsyncClient) -> None:
    """Base currency PKR. Test rates (rate_to_usd) from 2020-01-01: USD 1, EUR 1.2, AED 0.25, PKR 0.004.
    Extra rates: EUR 1.5 from 2026-10-10, PKR 0.005 from 2026-10-25. Days in between have NO
    row, so the latest earlier rate must be used."""
    await add_rate("EUR", date(2026, 10, 10), "1.5")
    await add_rate("PKR", date(2026, 10, 25), "0.005")
    h = await auth_headers(client, base="PKR")
    food = await make_category(client, h, "Food", budget="50000.00")
    travel = await make_category(client, h, "Travel")
    await make_category(client, h, "Empty")

    # October
    await make_expense(client, h, food, "100.00", "EUR", "2026-10-05")  # 100*1.2/0.004  = 30000
    await make_expense(client, h, food, "10.00", "EUR", "2026-10-12")  # no rate on 12th -> 1.5: 3750
    await make_expense(client, h, food, "1000.00", "PKR", "2026-10-07")  # same currency: 1000
    await make_expense(client, h, travel, "100.00", "AED", "2026-10-26")  # 100*0.25/0.005 = 5000
    await make_expense(client, h, travel, "20.00", "USD", "2026-10-25")  # 20*1/0.005 = 4000
    # September (previous month)
    await make_expense(client, h, food, "50.00", "USD", "2026-09-15")  # 50/0.004 = 12500
    # outside both months -> ignored
    await make_expense(client, h, food, "999.00", "USD", "2026-08-31")
    await make_expense(client, h, food, "999.00", "USD", "2026-11-01")

    r = await client.get("/reports/monthly", params={"year": 2026, "month": 10}, headers=h)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["base_currency"] == "PKR"
    assert report["total"] == "43750.00"
    rows = {row["category_name"]: row for row in report["categories"]}
    assert [row["category_name"] for row in report["categories"]] == ["Food", "Travel", "Empty"]

    assert rows["Food"] == {
        "category_id": food,
        "category_name": "Food",
        "total": "34750.00",
        "budget": "50000.00",
        "percent_used": "69.50",
        "previous_total": "12500.00",
        "change": "22250.00",
        "change_percent": "178.00",
    }
    assert rows["Travel"]["total"] == "9000.00"
    assert rows["Travel"]["previous_total"] == "0.00"
    assert rows["Travel"]["change_percent"] is None  # no division by zero
    assert rows["Travel"]["percent_used"] is None  # no budget
    assert rows["Empty"]["total"] == "0.00"


async def test_report_january_compares_with_previous_december(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    await make_expense(client, h, cat, "40.00", spent_on="2025-12-31")
    await make_expense(client, h, cat, "10.00", spent_on="2026-01-01")
    r = await client.get("/reports/monthly", params={"year": 2026, "month": 1}, headers=h)
    row = r.json()["categories"][0]
    assert (row["total"], row["previous_total"], row["change"], row["change_percent"]) == (
        "10.00",
        "40.00",
        "-30.00",
        "-75.00",
    )


async def test_report_validation_and_isolation(client: httpx.AsyncClient) -> None:
    alice = await auth_headers(client, "alice@example.com")
    bob = await auth_headers(client, "bob@example.com")
    cat = await make_category(client, alice)
    await make_expense(client, alice, cat, spent_on="2026-10-01")
    assert (await client.get("/reports/monthly?year=2026&month=13", headers=alice)).status_code == 422
    assert (await client.get("/reports/monthly?year=2026", headers=alice)).status_code == 422
    r = await client.get("/reports/monthly?year=2026&month=10", headers=bob)
    assert r.json() == {"year": 2026, "month": 10, "base_currency": "USD", "total": "0.00", "categories": []}


async def test_categories_list_shows_this_month_spend_vs_budget(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client, base="AED")
    food = await make_category(client, h, "Food", budget="100.00")
    await make_category(client, h, "Misc")
    today = datetime.now(UTC).date()
    await make_expense(client, h, food, "10.00", "USD", today.isoformat())  # 10 / 0.25 = 40 AED
    await make_expense(client, h, food, "5.00", "AED", today.isoformat())
    r = await client.get("/categories", headers=h)
    assert r.status_code == 200
    cats = {c["name"]: c for c in r.json()}
    assert cats["Food"]["spent_this_month"] == "45.00"
    assert cats["Food"]["percent_used"] == "45.00"
    assert cats["Misc"] == {
        "id": cats["Misc"]["id"],
        "name": "Misc",
        "monthly_budget": None,
        "spent_this_month": "0.00",
        "percent_used": None,
    }


async def test_category_names_are_unique_per_user_case_insensitive(client: httpx.AsyncClient) -> None:
    alice = await auth_headers(client, "alice@example.com")
    bob = await auth_headers(client, "bob@example.com")
    await make_category(client, alice, "Food")
    r = await client.post("/categories", json={"name": "FOOD"}, headers=alice)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "category_exists"
    await make_category(client, bob, "Food")  # other users may use the same name
    r = await client.post("/categories", json={"name": "X", "monthly_budget": "-1"}, headers=alice)
    assert r.status_code == 422
