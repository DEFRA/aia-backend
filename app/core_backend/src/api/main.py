import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from api.access_code import router as access_code_router
from api.cost_usage import router as cost_usage_router
from api.documents import router as documents_router
from api.health import router as health_router
from api.policy_documents import router as policy_documents_router
from api.users import router as users_router
from config import config
from utils.logger import get_logger
from utils.postgres import close_postgres_pool, init_db

logger = get_logger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if config.db.uri:
        try:
            await init_db()
            logger.info("PostgreSQL initialised")
        except Exception as exc:
            logger.warning(
                "PostgreSQL unavailable at startup — DB endpoints will fail: %s", exc
            )
    yield
    await close_postgres_pool()


app = FastAPI(title="AIA CoreBackend", version="1.0.0", lifespan=lifespan)

app.include_router(health_router)
app.include_router(documents_router, prefix=API_PREFIX)
app.include_router(users_router, prefix=API_PREFIX)
app.include_router(cost_usage_router, prefix=API_PREFIX)
app.include_router(policy_documents_router, prefix=API_PREFIX)
app.include_router(access_code_router, prefix=API_PREFIX)


def main() -> None:  # pragma: no cover
    if config.app.http_proxy:
        os.environ["HTTP_PROXY"] = str(config.app.http_proxy)
        os.environ["HTTPS_PROXY"] = str(config.app.http_proxy)
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)

    server_kwargs = {
        "app": "api.main:app",
        "host": config.app.host,
        "port": config.app.port,
        "reload": config.app.env.lower() in ("dev", "development"),
    }
    if config.app.log_config:
        server_kwargs["log_config"] = config.app.log_config

    uvicorn.run(**server_kwargs)


if __name__ == "__main__":  # pragma: no cover
    main()
