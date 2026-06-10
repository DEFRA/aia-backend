"""ECS Fargate Agent Service entry point.

Starts a FastAPI app with a /health endpoint for ECS health checks and
launches the SQS polling worker as a background asyncio task.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI

from app.agent_service.src.worker import run_worker
from app.agent_service.src.shared.app_config import config
from app.agent_service.src.shared.logger import get_logger

logger = get_logger("app.agent_service.main")

_WORKER_PORT = 8002

_worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    global _worker_task  # noqa: PLW0603
    _worker_task = asyncio.create_task(run_worker())
    logger.info("Agent service process started")
    yield
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except (asyncio.CancelledError, Exception):
            pass
    logger.info("Agent service process stopped")


app = FastAPI(title="AIA Agent Service", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(
        "app.agent_service.src.main:app",
        host=config.app.host,
        port=_WORKER_PORT,
        reload=config.app.env.lower() in ("dev", "development"),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.critical("Fatal agent service error: %s", exc)
        sys.exit(1)
