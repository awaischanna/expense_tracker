# Ahya Expense Tracker API

A fully async, multi-currency expense tracker built with **FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic, PostgreSQL and Redis**. Receipts are stored on S3. Locally (docker-compose and tests) a **moto** S3 server stands in for AWS; in production the same code talks to real S3.

| | |
|---|---|
| Tests | **73 passing**, real PostgreSQL + Redis, `pytest` (one command) |
| Coverage | **97%** (`pytest-cov`, see the table at the end of a test run) |
| Lint | `ruff check .` clean |

---

## 1. Quick start

### Option A: everything in Docker

```bash
cp .env.example .env          # then put real values in .env (it is git-ignored)
docker compose up --build
```

This starts PostgreSQL, Redis, a local S3 server (moto), the **API** on http://localhost:8000 and the **worker**. On start the API runs `alembic upgrade head` and creates the private receipts bucket if it is missing.

> **Why moto and not MinIO?** MinIO stopped publishing its free Docker images (`minio/mc` can no longer be pulled from Docker Hub). moto runs on the standard `python:3.12-slim` image, is the same S3 emulator the tests use, and supports pre-signed POST uploads. Only `S3_ENDPOINT_URL` is different from production.

Interactive docs are at http://localhost:8000/docs. The Postman collection is in `postman/`.

**Postman (`postman/` folder):**
- `expense-tracker.postman_collection.json`: every endpoint, in the order to run them. Login stores the token, and create calls store the new ids and ETag, so later requests just work.
- `s3-upload-only.postman_collection.json`: a single request for step 2 of the receipt flow (upload straight to S3). Run `upload-url` in Swagger, paste the whole response into the collection variable `uploadResponse`, pick a file in Body and Send (expect `204`), then run `confirm` in Swagger.
- `sample_import.csv`: a small CSV for `POST /expenses/import` (3 good rows, 3 bad rows, category `Food`).

**Trying the receipt upload by hand:** step 2 of the flow (uploading straight to S3) cannot be done from Swagger, because it is S3's endpoint, not ours. This helper runs all three steps for you:

```bash
python scripts/upload_receipt.py --token <access_token> --expense-id 1 --file path/to/bill.jpg
```

### Option B: run on your machine

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                       # point DATABASE_URL / REDIS_URL at your services
docker compose up -d postgres redis s3   # or use your own installs

alembic upgrade head                       # creates the schema + seeds 90 days of rates
uvicorn app.main:app --reload              # API
python -m app.worker.main                  # worker (second terminal)
```

### Migrations

| Command | What it does |
|---|---|
| `alembic upgrade head` | Apply everything. `0001` = schema and indexes, `0002` = exchange rates for PKR, AED, USD and EUR for the last 90 days. |
| `alembic downgrade base` | Remove everything. |
| `alembic check` | Confirms the models and the migrations match (it currently reports no difference). |

### Tests

```bash
pytest
```

The tests need PostgreSQL on `localhost:5432` and Redis on `localhost:6379`; `docker compose up -d postgres redis` is enough. They use the same `POSTGRES_USER` / `POSTGRES_PASSWORD` as docker-compose (read from your `.env`), so no extra setup is needed. If PostgreSQL is also installed directly on your computer it already owns port 5432; set `POSTGRES_PORT=5433` in `.env`, run `docker compose up -d postgres redis` again, and the tests will follow. You can override the locations with `TEST_DATABASE_URL` and `TEST_REDIS_URL`.

The test run does the following:
- It drops and re-creates the database `expenses_test`, then runs the real Alembic migrations, so the migrations are tested too.
- Before every test it truncates all tables, flushes Redis db 15 and inserts known exchange rates. Every test starts from a clean state.
- It starts a local **moto** S3 server. No real AWS is called.
- Coverage is printed automatically. CI (`.github/workflows/ci.yml`) fails below 85%.

### Troubleshooting

These were all hit while setting the project up on Windows, so they are listed with their fixes.

| Problem | Cause | Fix |
|---|---|---|
| `api-1` / `worker-1 exited with code 1` and `jwt_secret: String should have at least 32 characters` | `JWT_SECRET` in `.env` is too short. The app refuses weak secrets on purpose. | Put a random string of 32+ characters in `JWT_SECRET`, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `pull access denied for minio/mc` | MinIO no longer publishes its free images on Docker Hub. | Already fixed: local S3 is moto (`s3` service). Use the current `docker-compose.yml`. |
| `pytest` errors with `password authentication failed for user "postgres"`, although Docker works | PostgreSQL is also installed directly on the computer and owns `localhost:5432`, so the tests reach that server instead of the Docker one. (Inside Docker the API uses `postgres:5432`, which is why Docker still works.) | Add `POSTGRES_PORT=5433` to `.env`, run `docker compose up -d postgres redis`, then `pytest`. Data is kept. |
| Same password error and no other PostgreSQL installed | The Docker database was first created with a different `POSTGRES_PASSWORD`; a database keeps the password it was created with. | Put the original password back in `.env`, or reset the database (deletes local data): `docker compose down -v`, then `docker compose up -d postgres redis`. |
| `pip install -e ".[dev]"` fails with `Multiple top-level packages discovered` | Older `pyproject.toml` without package discovery settings. | Use the current `pyproject.toml` (it installs only `app`). |
| `.venv\Scripts\activate` fails: `running scripts is disabled on this system` | Windows PowerShell execution policy. | Run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. |
| `address already in use` on port `5055` during tests | Something else uses the port of the test S3 server (moto). | `$env:TEST_MOTO_PORT="5066"`, then `pytest`. |
| Postman says `Could not send request` / `ECONNREFUSED` | The web version of Postman cannot reach `localhost`, or Docker is not running. | Use the Postman desktop app and make sure `docker compose up` is running. |
| Receipt `confirm` returns `422 receipt_not_found` | Step 2 (upload to S3) was skipped. Swagger cannot do it, because the upload goes straight to S3, not to our API. | Use `scripts/upload_receipt.py` or `postman/s3-upload-only.postman_collection.json`, then call `confirm`. |
| Receipt `confirm` returns `422 invalid_file_type` | The file's real type does not match the `content_type` used in `upload-url` (e.g. a PNG sent as `image/jpeg`). This is the security check. | Request a new upload URL with the right `content_type` and upload again. |
| Creating an expense returns `422 no_exchange_rate` | `spent_on` is older than the seeded rates (last 90 days). | Use a recent date, or load older rates into `exchange_rates`. |

---

## 2. Project layout

```
app/
  main.py              app factory, routers, error handlers
  config.py            settings from environment variables only
  db.py, redis_client.py
  errors.py            the single error format + exception handlers
  models.py            SQLAlchemy 2.0 typed models (Decimal / NUMERIC for money)
  schemas.py           Pydantic v2 request/response models
  security.py          argon2 hashing (in a thread), JWT
  deps.py              current-user dependency (token_version check)
  storage.py           ALL S3 code (one module)
  api/v1/              thin HTTP layer: auth, categories, expenses (+receipts, import), reports
  services/            business logic: auth, rate_limit, expenses, idempotency, reports,
                       outbox (budget alerts), imports (CSV), receipts
  worker/main.py       background worker (outbox, imports, S3 cleanup, housekeeping)
alembic/versions/      0001 schema + indexes (each index commented), 0002 rate seed
tests/                 73 tests (pytest + httpx AsyncClient)
postman/               collection for every endpoint + a sample CSV
```

Routers stay thin: they parse input, call a service and shape the output. All the logic is in `services/`, which is also where the tests point most.

---

## 3. How each requirement is met

### Money
- Python always uses `Decimal`; the database uses `NUMERIC(14,2)` for amounts and `NUMERIC(20,10)` for rates.
- Pydantic validates `amount > 0`, at most 2 decimal places and at most 12 digits before the point. `NaN` and `Infinity` are rejected. A `CHECK (amount > 0)` in the database backs this up.
- Responses return money as **strings** (`"12.50"`), so no client ever parses a float.
- Currency conversion multiplies and divides in SQL `NUMERIC`. Only the final totals are rounded to 2 places, so rounding errors do not add up.

### Auth (`services/auth.py`)
- **Passwords:** argon2 via `argon2-cffi`. Hashing is CPU-heavy, so it runs in `asyncio.to_thread` and never blocks the event loop.
- **Timing:** an unknown email still verifies against a dummy hash, so response time does not reveal which emails exist.
- **Token lifetimes:** access tokens last 15 minutes and refresh tokens 7 days. Each refresh token has a row in the database, keyed by its `jti`.
- **Refresh rotation** is one conditional statement: `UPDATE refresh_tokens SET revoked_at=now() WHERE jti=? AND revoked_at IS NULL RETURNING user_id`. If two refreshes with the same token run in parallel, only one can match.
- **Reuse detection:**
  - If a token whose `revoked_reason='rotated'` comes back, someone kept a copy of it.
  - We then revoke **all** of that user's refresh tokens and bump `users.token_version`.
  - Every access token carries `tv` (the token version), so all existing access tokens die at once as well.
  - A token revoked by **logout** is just reported as invalid, because a client re-sending a logged-out token is not proof of theft.

### Login rate limit (`services/rate_limit.py`)
- The counter is in Redis, keyed by a hash of the email, so it is shared by every API server.
- A Lua script does **"reserve a slot, then check the password"** atomically:
  - A wrong password keeps its slot.
  - A right password gives the slot back.
  - When no slot is left, the API returns `429` with `Retry-After` (the key's remaining TTL).
- Because the check and the increment are one atomic step, 10 parallel wrong logins across 3 servers still get exactly 5 password checks. There is a test for this.
- The window is fixed and starts at the first failure.

### Data access
Every query filters on `user_id` **inside the SQL** (`WHERE id = ? AND user_id = ?`). Another user's row is therefore indistinguishable from a missing one and the API returns `404`, never `403`. This is tested for GET, PATCH and DELETE, and for using someone else's category.

### Listing with keyset pagination (`services/expenses.py::list_expenses`)
- **Filters:** `category_id`, `date_from`, `date_to`, `min_amount`, `max_amount` (in the expense's own currency), `currency` and `q`.
  - `q` is a case-insensitive substring search on the description.
  - `%` and `_` are escaped, so they match literally.
  - A `pg_trgm` GIN index makes the search fast.
- **Ordering and cursor:** results are ordered by `spent_on DESC, id DESC`. The cursor is base64 JSON of the last row's `(spent_on, id)`, and the next page is `WHERE (spent_on, id) < (:s, :i)`.
- **Why rows inserted between pages cannot cause duplicates or gaps:** the position is a value, not a count. The test inserts newer, same-day and older rows in the middle of paging.
- **Page detection:** we fetch `limit + 1` rows to know whether there is a next page, with no `COUNT(*)`.

### Errors (`errors.py`)
Every error has the shape `{"error": {"code", "message", "details"}}`. That includes FastAPI validation errors, 404s for unknown routes and the catch-all 500.

| Status | When |
|---|---|
| 400 | Bad cursor, bad If-Match, empty file or empty Idempotency-Key |
| 401 | Bad or missing token |
| 404 | Missing or not yours |
| 409 | Duplicate email or category |
| 412 | Version conflict |
| 422 | Validation error |
| 428 | Missing If-Match |
| 429 | Rate limited |

### Monthly report (`services/reports.py`)
**One SQL statement**, built from CTEs:
1. `converted`:
   - Each expense is joined with two `LEFT JOIN LATERAL` lookups: the latest rate `valid_on <= spent_on` for the expense currency and for the user's base currency.
   - Each lookup is `ORDER BY valid_on DESC LIMIT 1`, which is one backward probe of the `(currency, valid_on)` primary key.
   - The formula is `amount * rate(cur) / rate(base)`.
   - If a day has no rate, the latest earlier rate is used automatically.
2. `grid`: categories `CROSS JOIN` the two months, `LEFT JOIN` the converted rows, then `GROUP BY`. A month with no spend becomes 0, not a missing row.
3. `LAG(total) OVER (PARTITION BY category ORDER BY month)` gives last month's total. This works for January too: it compares with the previous December, and there is a test for it.
4. `SUM(total) OVER ()` gives the grand total, still in the same query.

For each category the report returns `total`, `budget`, `percent_used`, `previous_total`, `change` and `change_percent`. `change_percent` is `null` when last month was 0, to avoid division by zero.

`GET /categories` uses the same conversion CTE in one query to show this month's spend against the budget.

**No rate at all for a date:** creating an expense dated before the first day that has rates for every currency returns `422 no_exchange_rate`. So the report never silently drops an expense.

### Migrations and indexes
Every index in `alembic/versions/0001_initial_schema.py` has a comment naming the query that needs it. In short:

| Index | Used by |
|---|---|
| `expenses (user_id, spent_on DESC, id DESC)` | `GET /expenses`. Same order as the `ORDER BY`, so Postgres walks the index and stops after `limit` rows, with no sort. |
| `expenses (user_id, category_id, spent_on)` | Report, category list and budget worker (one user, one category, a date range). It also covers the FK check on `category_id`. |
| `expenses` GIN `description gin_trgm_ops` | The `q` filter (`ILIKE '%x%'` cannot use a b-tree). |
| `expenses (user_id, import_key)` unique, partial `WHERE import_key IS NOT NULL` | CSV de-duplication. Manual expenses cost nothing in this index. |
| `exchange_rates` PK `(currency, valid_on)` | "Latest rate on or before date X". |
| `categories` unique `(user_id, lower(name))` | Case-insensitive 409, and the CSV name-to-id lookup. |
| `users.email` unique, `refresh_tokens.jti` unique, `refresh_tokens.user_id` | Login, refresh, and "revoke all". |
| `idempotency_keys` PK `(user_id, key)`, `expires_at` | Race safety (see 2.1) and purging. |
| `outbox_events (id) WHERE processed_at IS NULL` | Worker polling. A partial index stays tiny with millions of processed rows. |
| `notifications` unique `(user_id, category_id, period, level)`, `(user_id, id DESC)` | "Only once", and the listing. |
| `import_jobs` unique `(user_id, file_hash)`, partial `pending` | Duplicate upload detection, and the worker queue. |

### 2.1 Idempotency-Key (`services/idempotency.py`)
1. In the request's transaction we first `INSERT` the key row (`ON CONFLICT DO UPDATE ... WHERE expires_at < now() RETURNING`).
2. If we inserted it, we create the expense and store the response on the key row, then commit **everything in one transaction**.
3. A parallel request with the same key runs the same INSERT. **PostgreSQL makes it wait** on the unique index until the first transaction finishes:
   - If the first one committed, the second gets a conflict, reads the stored response and replays it.
   - If the body hash is different, it returns `422`.
   - If the first one rolled back (for example a 404 category), the second request's insert succeeds and it does the work. A failed request never "burns" a key.
4. A replayed response has the same status and body, plus an `Idempotency-Replayed: true` header.
5. Keys are scoped per user and live for 24 hours. An expired key can be re-claimed by the same upsert, and the worker purges old rows.

**Why the DB constraint and not a Redis lock?**
- The unique constraint is part of the same transaction as the expense, so "expense created" and "key recorded" cannot disagree.
- A Redis lock has a TTL. If the first request takes longer than the TTL, both requests run.
- If Redis is down, a lock forces us to choose between failing every POST and being unsafe.

The test fires 5 identical requests with `asyncio.gather` and gets one expense.

### 2.2 No lost updates (`services/expenses.py::update_expense`)
- `GET` returns `ETag: "<version>"`.
- `PATCH` requires `If-Match`: a missing header returns `428` and a malformed one returns `400`.
- The version check and the write are **one** statement: `UPDATE expenses SET ..., version = version + 1 WHERE id=? AND user_id=? AND version=? RETURNING *`.
  - When two PATCHes race, Postgres blocks the second on the row lock and then re-evaluates `version = ?` against the committed row. It no longer matches, so 0 rows are updated and the API returns `412`.
  - The 412 response includes the current `ETag`.
  - The test runs two PATCHes in parallel and gets exactly one 200 and one 412.
- Confirming a receipt also bumps the version.

### 2.3 Budget alerts with a transactional outbox (`services/outbox.py`)
- **API side:**
  - Creating or editing an expense in a category that has a budget adds an `outbox_events` row in the **same transaction** as the expense.
  - The request itself sends nothing. If the transaction rolls back, the event disappears with it.
- **Worker side:**
  - The worker claims a batch with `SELECT ... FOR UPDATE SKIP LOCKED`, so parallel workers never take the same event.
  - It groups events by (user, category, month) and computes that month's total once, in SQL, in base currency.
  - It inserts a notification for each level reached (80 and 100) with `ON CONFLICT DO NOTHING` on `UNIQUE (user_id, category_id, period, level)`.
  - It marks the events processed, all in one transaction.
- **Exactly one alert:**
  - If the worker crashes before committing, nothing is saved and the events are retried.
  - If an event is replayed after it committed, the unique constraint makes the insert a no-op.
  - If two workers run, SKIP LOCKED and the constraint together keep it to one alert. Tests cover replays, a direct second call and 4 parallel workers.
- **Failures:** a group that fails is retried up to 10 times inside its own SAVEPOINT. After that it is marked done with `last_error` kept, which acts as a simple dead letter.
- `GET /notifications` lists newest first, with a `before_id` cursor.

**What happens when an expense is edited or deleted.** An alert means "you crossed X% this month". It is a fact about a moment, not a live gauge.
- **Edit that increases the total**, or moves the expense to another category or month: a new event is written for the expense's new category and month, and the worker re-checks it.
- **Delete, or an edit that lowers the total:** nothing is sent, and existing alerts are **not** retracted. The user really did cross the line, and `GET /categories` always shows the live percentage.
- **Dropping below and crossing again in the same month** does not send a second alert for the same level. That is what "once per category per month per level" means. A different month or a higher level does produce a new alert.
- To support "un-alerting" later, we would add a `retracted_at` column and have the worker set it when the total drops below the level.

### 2.4 Bulk CSV import (`services/imports.py`)
- **File format:** a header row, then one expense per line. `description` is optional, and `category` is the category **name** (case-insensitive), which must already exist.
  ```
  date,amount,currency,category,description
  2026-10-01,2500.00,PKR,Food,Weekly groceries
  2026-10-03,45,AED,Travel,"Taxi, airport"
  ```
- **Upload:**
  - `POST /expenses/import` streams the upload to disk in 1 MB chunks while computing SHA-256, so the whole file is never in memory.
  - It inserts an `import_jobs` row and returns **202** with `job_id`.
  - `GET /imports/{job_id}` shows `status`, `rows_total`, `rows_done`, `rows_failed` and `errors`.
- **Processing in the worker:**
  - The worker is a separate process, so the API keeps serving other requests.
  - It reads the file with `csv.DictReader` in a thread, `1000` rows at a time.
  - It validates each row: date, amount (> 0, at most 2 decimals), currency, and category name (case-insensitive).
  - It inserts the good rows of each batch with **one multi-row `INSERT ... ON CONFLICT DO NOTHING`** and commits per batch, so progress is visible and memory stays flat.
  - Bad rows go to `errors` with their **file line number** (the header is line 1) and the reason.
- **No duplicates:**
  - The same file uploaded twice hits `UNIQUE (user_id, file_hash)` and gets the first job back (`duplicate_of_previous_upload: true`).
  - Every row also gets `import_key = "<sha256>:<line>"` with a unique index. If the worker dies half way and the job is re-queued (stale heartbeat after 10 minutes), the rows already saved are skipped.
- **Limits:** at most 100,000 data rows (extra rows are reported as failed) and at most 50 MB per file.
- **Budget alerts:** each batch writes outbox events for the budgeted categories and months it touched.
- **Why not COPY:** COPY has no `ON CONFLICT`, which the crash-safe dedupe relies on. The faster variant would be `COPY` into a temp table, then `INSERT ... SELECT ... ON CONFLICT DO NOTHING`. Multi-row inserts were fast enough. **Measured:** a 100,000-row file (3.7 MB) got its `202` in about 80 ms, and the worker imported all rows in about 28 seconds on a single-vCPU container with Postgres on the same machine.

### 2.5 Receipts on S3 (`storage.py`, `services/receipts.py`)
- **Upload URL:**
  - `upload-url` returns a **pre-signed POST**, not a PUT. Only a POST policy can enforce a size limit.
  - The policy pins the exact key, the `Content-Type` and `content-length-range 1..5 MB`.
  - Allowed types are JPG, PNG and PDF.
  - The key has the form `receipts/{user_id}/{expense_id}/{uuid}.{ext}`.
- **Confirm:**
  - The key must match that exact user and expense prefix, so a client cannot attach someone else's file.
  - We `HEAD` the object to check that it exists and is at most 5 MB.
  - We then do a ranged GET of the **first 16 bytes** and check the magic numbers (`FF D8 FF`, `89 50 4E 47 0D 0A 1A 0A`, `%PDF-`). The real type must match the extension.
  - A file that fails is deleted and the API returns `422`. The test uploads an `.exe` renamed to `.png`.
- **Download:** a pre-signed GET valid for **15 minutes**.
- **Private bucket:** the bucket is created with no public ACL or bucket policy (on AWS: Block Public Access fully on).
- **Delete:**
  - Deleting an expense inserts an `s3_deletions` row **in the same transaction**.
  - After commit the API tries the delete once. If S3 fails, the row stays and the worker retries with exponential backoff (2 seconds up to 1 hour).
  - Replacing a receipt queues the old file the same way.
  - The test covers this: it simulates an S3 outage and checks that the file is cleaned up later.
- All S3 calls are in `app/storage.py`. On AWS no keys are configured and the IAM role is used.

---

## 4. What I did not finish / would do next

| Item | Current state | How I would finish it |
|---|---|---|
| Where uploaded CSVs live | A shared docker volume between API and worker. Fine for one host. | Stream the upload to S3 (`imports/{user}/{job}.csv`) and let the worker stream it back with ranged reads. Then API and worker can run on different machines. |
| Error list size for imports | The first 1000 row errors are stored (`rows_failed` still counts all of them). | Write errors to an `import_errors` table or a CSV in S3 and serve it from a download link. |
| Orphan receipt uploads (URL generated, never confirmed) | Not cleaned up. | Upload with an `x-amz-tagging: status=pending` field, remove the tag on confirm, and add an S3 lifecycle rule that deletes objects still tagged `pending` after 1 day. |
| Re-importing a file after fixing its problems (for example, creating a missing category) | The same file is recognised by its hash and the first job is returned, so nothing is imported again. Changing the file and uploading it again would re-insert the rows that already succeeded, because row de-duplication is per file. | Offer the failed rows as a downloadable CSV, so the user fixes and re-uploads only those rows. Alternatively, add an explicit `POST /imports/{job_id}/retry` that re-runs a job: rows already saved are skipped by their `import_key`. |
| Exchange rates | 90 days of seeded sample data. | A daily worker job that loads real rates from a provider. |
| Category update/delete endpoints | Not in the spec, so not built. Expenses `RESTRICT` deletion of a used category. | `PATCH`/`DELETE /categories/{id}` with the same 404 rules. |
| Observability | Plain logging. | JSON logs with a request id, Prometheus metrics (request latency, outbox lag, import throughput), and Sentry. |
| Type checking | `mypy` is configured but not enforced in CI. | Make it pass in strict mode and add it to CI. |

---

## 5. AI usage

I used **Claude** (Anthropic's AI assistant, Claude Opus 5.5) a lot in this project. To be clear about where:

**What Claude did**
- Read the assessment PDF and wrote most of the first version of the code: the FastAPI app, models, Alembic migrations, services (auth, rate limit, idempotency, optimistic locking, outbox, CSV import, receipts), the worker, and the SQL for the monthly report.
- Wrote the pytest suite and ran it against real PostgreSQL and Redis until it passed with 97% coverage.
- Wrote the README (including the deployment notes and short answers), the Dockerfile, docker-compose, the Postman collections, the sample CSV and the `upload_receipt.py` helper script.

**What I did**
- Ran the whole stack on my own machine (Windows + Docker) and fixed the setup problems with Claude's help. For example, the `minio/mc` image can no longer be pulled from Docker Hub, so local S3 was switched to moto, and my first `JWT_SECRET` was too short.
- Tested every endpoint by hand in Swagger and Postman: auth, categories, expenses, Idempotency-Key, receipt upload to S3, CSV import and notifications.
- While testing the CSV import I found that re-uploading a file after creating a missing category does not import it again. This is now listed in section 4 with how I would fix it.
- Asked Claude to explain the parts I did not understand (idempotency race, ETag/If-Match, outbox, keyset pagination, pre-signed URLs) and went through the code to understand them.

## 6. Deployment notes (AWS)

### 6.1 API and worker: ECS on Fargate
- **Image:** one image, two ECS services.
  - **api:** at least 2 tasks (one per AZ), behind an ALB, with health checks on `/health`.
  - **worker:** 1 to N tasks, with no load balancer.
- **Restarts:** ECS restarts a task when it crashes or fails its health check.
- **Scaling:**
  - The API scales on CPU (target 60%) and ALB request count.
  - The worker scales on **outbox lag**, a custom CloudWatch metric equal to `count(*) WHERE processed_at IS NULL`, and on the number of pending imports.
- **Zero-downtime release:** rolling deployment with `minimumHealthyPercent=100` and `maximumPercent=200`.
  - New tasks start and must pass the ALB health check.
  - Only then does the ALB drain the old tasks (a deregistration delay of about 30 seconds lets in-flight requests finish).
  - Workers get `SIGTERM`. Our loop finishes the current batch, whose transaction commits, and exits. Anything not committed is simply re-claimed by another worker.
  - The ECS deployment circuit breaker with rollback undoes a release that fails its health checks.

### 6.2 Database: RDS PostgreSQL 16
- Multi-AZ, in **private subnets** only, with no public IP.
- **Security group `rds-sg`:** inbound **TCP 5432 only from `api-sg` and `worker-sg`** (security-group references, not CIDRs). There is no other inbound traffic, and the outbound default is fine.
- **Password:**
  - Stored in **Secrets Manager**, using RDS-managed rotation.
  - The ECS task definition maps it with `secrets: [{name: DATABASE_PASSWORD, valueFrom: <secret ARN>}]`, so ECS injects it at start-up.
  - The task *execution* role has `secretsmanager:GetSecretValue` on that ARN only.
  - Nothing is in the image or in git. `JWT_SECRET` is handled the same way.
- **Connections:** use RDS Proxy (or PgBouncer) when tasks scale out, so the number of connections stays bounded.

### 6.3 Redis: ElastiCache
- ElastiCache for Redis with a replica and automatic failover. It sits in private subnets with a security group that allows 6379 from the API only.
- **If Redis goes down:**
  - **Rate limits:** the login endpoint cannot count failures. I would **fail closed for login**: return `503` with a short `Retry-After`. Logging in briefly is less important than allowing unlimited password guessing. Every other endpoint works normally, because only login touches Redis.
  - **Idempotency keys:** nothing happens. They are stored in **PostgreSQL**, not Redis, which is one of the reasons for that design.
  - **Outbox, imports and S3 cleanup:** nothing happens either. The worker coordinates through Postgres only.

### 6.4 S3
- Private bucket with **Block Public Access** fully on, SSE-S3 (or SSE-KMS) encryption, a CORS rule allowing `POST` from the app's origins, and a lifecycle rule for orphaned uploads (see section 4).
- The tasks use their **IAM task role**, not access keys. Smallest policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReceiptObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::expense-receipts/receipts/*"
    }
  ]
}
```

- `PutObject` is what signs the pre-signed POST. `GetObject` covers the confirm check (HEAD and ranged GET) and the download URL. `DeleteObject` covers rejected files and cleanup.
- `ListBucket` is not needed. Without it, a HEAD on a missing object returns 403 instead of 404, which our code also treats as "not uploaded".
- When CSVs move to S3, add the same three actions on `.../imports/*`.

### 6.5 Migrations during a release (expand / contract)
- Migrations run as a **one-off ECS task** (`alembic upgrade head`) **before** the new service version rolls out, so they run once and not in every container.
- Every migration must work with **both** the old and the new code, because both run during the rollout:
  1. **Expand** (release N): add nullable columns, new tables and indexes. Indexes use `CREATE INDEX CONCURRENTLY` so tables are not locked. The old code ignores the new columns.
  2. **Migrate:** deploy code that writes both the old and the new shape. Backfill in small batches.
  3. **Contract** (release N+1 or later): once no old code is running, drop the old column or make the new one `NOT NULL`.
- Never rename or drop a column in the same release that stops using it. Set a `lock_timeout` in migrations so a blocked DDL fails fast instead of queuing every query behind it.

---

## 7. Short answers

**1. Why pre-signed upload URLs, what can go wrong, and how to guard against it?**
- **Why:** the file goes straight from the phone to S3. API servers do not hold 5 MB bodies in memory, do not tie up connections on slow mobile uploads, and do not pay for the bandwidth twice. S3 scales the uploads for us.
- **What can go wrong:**
  - A leaked URL can be used by someone else until it expires.
  - A client can upload something other than what it claimed: wrong type, malware, or a huge file.
  - Uploads can be orphaned and never confirmed.
  - The key can be guessed, or swapped for another user's key.
- **Guards:**
  - URLs are short-lived (10 minutes) and signed for one exact key.
  - A POST policy enforces `content-length-range` and `Content-Type`.
  - Keys contain a random UUID and the user and expense ids, and confirm checks that prefix.
  - Confirm **re-checks the real bytes** (magic numbers) and the size, and deletes bad files.
  - The bucket is private, downloads are pre-signed GETs valid for 15 minutes, and a lifecycle rule removes unconfirmed objects.
  - For stronger protection we could add a virus scan through an S3 event to Lambda before the file is marked "clean".

**2. `GET /expenses` is slow at 20 million rows. How do you find and fix it?**
1. **Find the slow query:** use `pg_stat_statements` (by total and mean time), or `log_min_duration_statement`.
2. **Run `EXPLAIN (ANALYZE, BUFFERS)` with real parameters.** Look for:
   - a `Seq Scan`, or a `Sort` node, on expenses;
   - "Rows Removed by Filter" in the millions;
   - estimated rows far from actual rows, which means stale statistics (`ANALYZE`);
   - high `shared read` buffers compared to `shared hit`, which means data comes from disk.
3. **Fix with the right composite index:** `(user_id, spent_on DESC, id DESC)`.
   - The equality column comes first, then the ORDER BY columns in the same order and direction.
   - Postgres then reads exactly `limit` rows from the index, with no sort and without touching other users' rows.
   - Filters like `category_id` can get their own composite index (`user_id, category_id, spent_on DESC, id DESC`) if they are common. `q` needs the trigram GIN index.
4. **Why offset pagination gets slow:** `OFFSET 1000000` still makes Postgres produce and throw away a million rows, so each page gets slower. It also skips or repeats rows when data changes. A keyset condition `(spent_on, id) < (?, ?)` is a single index seek, the same cost on page 1 and page 50,000.
5. **N+1 queries:** if the serializer loaded each expense's category (or receipt) lazily, a 100-item page would run 101 queries. Watch the SQL logs or count queries in a test, and fix it with a join, or `selectinload` for collections. Here the list endpoint is one query.
6. If it is still slow, check `work_mem` and table bloat (autovacuum), add a covering index (`INCLUDE`) for index-only scans, and partition by date as a last step.

**3. Three API servers behind a load balancer: what breaks and how it was made safe?**
- **Tokens:** JWTs are stateless, so any server can verify them. Revocation state (refresh rows and `token_version`) is in Postgres, which all servers share. Rotation is a single conditional UPDATE, so two servers cannot both rotate the same token.
- **Rate limits:** an in-memory counter would allow 5 attempts *per server*, or 15 in total. The counter is in Redis and is changed atomically by a Lua script, so the limit is global and parallel requests cannot slip past it.
- **Worker:** running several workers would double-process events without protection. Here they claim rows with `FOR UPDATE SKIP LOCKED`, and every effect is protected by a unique constraint, so any number of workers is safe.
- **Idempotency:** the same key can hit two different servers. The Postgres unique index makes the second one wait and then replay the stored response.
- **File uploads:** files never go through the API, so there is no local disk state for receipts. CSV imports currently use a shared volume. On several hosts they must go to S3 (listed in section 4).
- **Migrations:** if every container ran `alembic upgrade` on start, three of them would race. Migrations run once, as a separate task before the rollout, and follow the expand/contract rules.

**4. The outbox worker was down for 2 hours and 500,000 events piled up. What happens, and how do you avoid overloading the database?**
- **When it comes back:** nothing is lost, because the events are rows in Postgres. The worker drains them in `ORDER BY id` batches of 100.
- **Coalescing:** each batch is grouped by (user, category, month) and each group is checked **once**. 500k events usually belong to far fewer category-months, so most of them collapse into one query each.
- **Cheap polling:** the partial index on unprocessed events keeps finding the next batch fast.
- **Limiting load:**
  - Keep batch size and the number of worker replicas modest (each worker uses one connection).
  - Add a small sleep between batches when the backlog is large, or watch replica lag and DB CPU and back off.
  - Run the worker against RDS Proxy so it cannot exhaust connections.
  - During a backlog, scale workers out slowly rather than to the maximum.
- **Duplicates:** none. Replays and overlapping workers are absorbed by the unique constraint.
- **Housekeeping:** processed events are deleted after 7 days so the table does not grow without bound.
- **A further step** would be de-duplicating at write time with `UNIQUE (user, category, period) WHERE processed_at IS NULL` and `ON CONFLICT DO NOTHING`, so a burst creates one pending event instead of thousands.

**5. One place where I chose simple over "perfect".**
- **The choice:** the background worker is a **plain asyncio loop that polls Postgres**, with `FOR UPDATE SKIP LOCKED`, instead of Celery or arq with a broker.
- **What we give up:** a "perfect" setup would push events through a broker for instant delivery, built-in retries, scheduling and a dashboard. Polling adds up to about 1 second of latency, and we wrote our own small retry and backoff.
- **What we gain:**
  - The outbox **already is** in Postgres, so the queue and the data share one transaction and one source of truth.
  - There is no second system to keep consistent and no "message sent but transaction rolled back" bugs.
  - It is about 100 lines that anyone can read in the review.
  - Scaling is simply "run more replicas".
- **For a budget alert, one second of delay is irrelevant.** If we needed lower latency, we could add `LISTEN/NOTIFY` to wake the worker without changing anything else.#   e x p e n s e _ t r a c k e r  
 