"""Application settings.

All values come from environment variables (or a local, git-ignored .env file).
Nothing secret has a real default: the app refuses to start without JWT_SECRET.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Ahya Expense Tracker"
    environment: str = "local"

    # Database / Redis
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/expenses"
    db_pool_size: int = 10
    db_null_pool: bool = False  # tests use NullPool so no connection outlives an event loop
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    jwt_secret: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 7 * 24 * 60 * 60

    # Login rate limit
    login_max_failures: int = 5
    login_window_seconds: int = 15 * 60

    # Idempotency
    idempotency_ttl_seconds: int = 24 * 60 * 60

    # Money / currencies
    supported_currencies: list[str] = ["PKR", "AED", "USD", "EUR"]

    # S3 (real AWS, or a local emulator such as moto)
    s3_bucket: str = "expense-receipts"
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None  # set for a local emulator (moto/LocalStack); None on real AWS
    s3_public_endpoint_url: str | None = None  # URL clients use for pre-signed links (local emulator in docker)
    aws_access_key_id: str | None = None  # only for a local emulator; on AWS an IAM role is used
    aws_secret_access_key: str | None = None
    receipt_max_bytes: int = 5 * 1024 * 1024
    upload_url_ttl_seconds: int = 10 * 60
    download_url_ttl_seconds: int = 15 * 60

    # CSV import
    import_dir: str = "/tmp/expense-imports"
    import_max_rows: int = 100_000
    import_batch_size: int = 1_000

    # Worker
    worker_poll_seconds: float = 1.0
    outbox_batch_size: int = 100

    # Pagination
    page_size_default: int = 20
    page_size_max: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
