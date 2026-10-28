"""Generates postman/expense-tracker.postman_collection.json (run: python scripts/build_postman.py)."""

import json
from pathlib import Path

AUTH = {"type": "bearer", "bearer": [{"key": "token", "value": "{{accessToken}}", "type": "string"}]}


def req(name, method, path, body=None, headers=None, auth=True, tests=None, query=None, formdata=None):
    url = {
        "raw": "{{baseUrl}}" + path,
        "host": ["{{baseUrl}}"],
        "path": [p for p in path.split("?")[0].split("/") if p],
    }
    if query:
        url["query"] = [{"key": k, "value": v, "disabled": d} for k, v, d in query]
        url["raw"] += "?" + "&".join(f"{k}={v}" for k, v, d in query if not d)
    r = {"method": method, "header": [{"key": k, "value": v} for k, v in (headers or {}).items()], "url": url}
    if body is not None:
        r["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2), "options": {"raw": {"language": "json"}}}
    if formdata is not None:
        r["body"] = {"mode": "formdata", "formdata": formdata}
    r["auth"] = AUTH if auth else {"type": "noauth"}
    item = {"name": name, "request": r}
    if tests:
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": tests}}]
    return item


save_tokens = [
    "if (pm.response.code === 200) {",
    "  const j = pm.response.json();",
    "  pm.collectionVariables.set('accessToken', j.access_token);",
    "  pm.collectionVariables.set('refreshToken', j.refresh_token);",
    "}",
]

collection = {
    "info": {
        "name": "Ahya Expense Tracker API",
        "description": "Run the requests top to bottom. Login/refresh store tokens in collection variables.",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "variable": [
        {"key": "baseUrl", "value": "http://localhost:8000/api/v1"},
        {"key": "accessToken", "value": ""},
        {"key": "refreshToken", "value": ""},
        {"key": "categoryId", "value": "1"},
        {"key": "expenseId", "value": "1"},
        {"key": "etag", "value": '"1"'},
        {"key": "jobId", "value": ""},
        {"key": "receiptKey", "value": ""},
    ],
    "item": [
        {
            "name": "Auth",
            "item": [
                req(
                    "Register",
                    "POST",
                    "/auth/register",
                    {"email": "demo@example.com", "password": "password123", "base_currency": "PKR"},
                    auth=False,
                ),
                req(
                    "Login",
                    "POST",
                    "/auth/login",
                    {"email": "demo@example.com", "password": "password123"},
                    auth=False,
                    tests=save_tokens,
                ),
                req(
                    "Refresh",
                    "POST",
                    "/auth/refresh",
                    {"refresh_token": "{{refreshToken}}"},
                    auth=False,
                    tests=save_tokens,
                ),
                req("Logout", "POST", "/auth/logout", {"refresh_token": "{{refreshToken}}"}, auth=False),
            ],
        },
        {
            "name": "Categories",
            "item": [
                req(
                    "Create category",
                    "POST",
                    "/categories",
                    {"name": "Food", "monthly_budget": "50000.00"},
                    tests=[
                        "if (pm.response.code === 201) pm.collectionVariables.set('categoryId', pm.response.json().id);"
                    ],
                ),
                req("List categories (spend vs budget)", "GET", "/categories"),
            ],
        },
        {
            "name": "Expenses",
            "item": [
                req(
                    "Create expense (idempotent)",
                    "POST",
                    "/expenses",
                    {
                        "category_id": "{{categoryId}}",
                        "amount": "1250.50",
                        "currency": "PKR",
                        "description": "Lunch",
                        "spent_on": "2026-10-20",
                    },
                    headers={"Idempotency-Key": "{{$guid}}"},
                    tests=[
                        "if (pm.response.code === 201) {",
                        "  pm.collectionVariables.set('expenseId', pm.response.json().id);",
                        "  pm.collectionVariables.set('etag', pm.response.headers.get('ETag'));",
                        "}",
                    ],
                ),
                req(
                    "List expenses",
                    "GET",
                    "/expenses",
                    query=[
                        ("limit", "20", False),
                        ("cursor", "", True),
                        ("category_id", "{{categoryId}}", True),
                        ("date_from", "2026-10-01", True),
                        ("date_to", "2026-10-31", True),
                        ("min_amount", "10", True),
                        ("max_amount", "5000", True),
                        ("currency", "PKR", True),
                        ("q", "lunch", True),
                    ],
                ),
                req(
                    "Get expense (returns ETag)",
                    "GET",
                    "/expenses/{{expenseId}}",
                    tests=["pm.collectionVariables.set('etag', pm.response.headers.get('ETag'));"],
                ),
                req(
                    "Update expense (If-Match)",
                    "PATCH",
                    "/expenses/{{expenseId}}",
                    {"amount": "1300.00", "description": "Lunch with team"},
                    headers={"If-Match": "{{etag}}"},
                    tests=[
                        "if (pm.response.code === 200) pm.collectionVariables.set('etag', pm.response.headers.get('ETag'));"
                    ],
                ),
                req("Delete expense", "DELETE", "/expenses/{{expenseId}}"),
            ],
        },
        {
            "name": "Receipts",
            "item": [
                req(
                    "Get upload URL",
                    "POST",
                    "/expenses/{{expenseId}}/receipt/upload-url",
                    {"content_type": "image/png"},
                    tests=[
                        "if (pm.response.code === 200) pm.collectionVariables.set('receiptKey', pm.response.json().key);"
                    ],
                ),
                req("Confirm upload", "POST", "/expenses/{{expenseId}}/receipt/confirm", {"key": "{{receiptKey}}"}),
                req("Get download URL (15 min)", "GET", "/expenses/{{expenseId}}/receipt"),
            ],
        },
        {
            "name": "CSV import",
            "item": [
                req(
                    "Upload CSV",
                    "POST",
                    "/expenses/import",
                    formdata=[{"key": "file", "type": "file", "src": "postman/sample_import.csv"}],
                    tests=[
                        "if (pm.response.code === 202) pm.collectionVariables.set('jobId', pm.response.json().job_id);"
                    ],
                ),
                req("Import status", "GET", "/imports/{{jobId}}"),
            ],
        },
        {
            "name": "Reports & notifications",
            "item": [
                req(
                    "Monthly report", "GET", "/reports/monthly", query=[("year", "2026", False), ("month", "10", False)]
                ),
                req("Notifications", "GET", "/notifications", query=[("limit", "50", False), ("before_id", "", True)]),
            ],
        },
    ],
}

out = Path(__file__).resolve().parent.parent / "postman"
(out / "expense-tracker.postman_collection.json").write_text(json.dumps(collection, indent=2))
print("written")
