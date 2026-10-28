from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.api.v1 import auth, categories, expenses, reports
from app.config import get_settings
from app.db import dispose_engine
from app.errors import register_error_handlers
from app.redis_client import close_redis


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title=get_settings().app_name, version="1.0.0", lifespan=lifespan)
    register_error_handlers(app)

    api = APIRouter(prefix="/api/v1")
    for module in (auth, categories, expenses, reports):
        api.include_router(module.router)
    app.include_router(api)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
