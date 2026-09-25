"""ASGI entry point for repository and analysis APIs.

Run with ``uvicorn main:app`` from backend/ or ``uvicorn backend.main:app`` from
the repository root. When DATABASE_URL is set, the same process also runs the
analysis worker that moves QUEUED analyses through the pipeline.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

try:
    from cors_config import allowed_origins
    from routers.analyses import configure_connection_factory
    from routers.analyses import router as analyses_router
    from services.analysis_pipeline import run_analysis_pipeline
    from services.analysis_worker import supervise_worker
    from services.postgres import PostgresUnavailable, make_connection_factory
except ModuleNotFoundError:
    from backend.cors_config import allowed_origins
    from backend.routers.analyses import configure_connection_factory
    from backend.routers.analyses import router as analyses_router
    from backend.services.analysis_pipeline import run_analysis_pipeline
    from backend.services.analysis_worker import supervise_worker
    from backend.services.postgres import PostgresUnavailable, make_connection_factory

load_dotenv(Path(__file__).resolve().with_name(".env"))
# No-op when the host already configured logging; otherwise worker progress would be invisible.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

WORKER_SHUTDOWN_SECONDS = 10.0


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _worker_options() -> dict:
    return {
        "poll_seconds": float(os.getenv("ANALYSIS_WORKER_POLL_SECONDS", "1.0")),
        "stale_attempt_seconds": float(os.getenv("ANALYSIS_ATTEMPT_STALE_SECONDS", "1800")),
        "max_attempts": int(os.getenv("ANALYSIS_MAX_ATTEMPTS", "3")),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Configure PostgreSQL persistence and run the analysis worker alongside the API."""
    app.state.connection_factory = None
    app.state.worker_task = None
    stop_event = asyncio.Event()

    if os.getenv("DATABASE_URL"):
        try:
            app.state.connection_factory = make_connection_factory()
            configure_connection_factory(app.state.connection_factory)
        except PostgresUnavailable as exc:
            # The API remains importable for local resolver-only development.
            logger.warning("Analysis persistence is unavailable: %s", exc)
    else:
        logger.warning("DATABASE_URL is not set; analyses cannot be created or processed")

    if app.state.connection_factory is not None:
        if _env_flag("ANALYSIS_WORKER_ENABLED", True):
            app.state.worker_task = asyncio.create_task(
                supervise_worker(
                    app.state.connection_factory,
                    run_analysis_pipeline,
                    stop_event=stop_event,
                    **_worker_options(),
                ),
                name="analysis-worker",
            )
        else:
            logger.warning("ANALYSIS_WORKER_ENABLED is off; QUEUED analyses will not be processed by this process")

    try:
        yield
    finally:
        task = app.state.worker_task
        if task is not None:
            stop_event.set()
            try:
                await asyncio.wait_for(task, timeout=WORKER_SHUTDOWN_SECONDS)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()


main = FastAPI(
    title="StackSniffer API",
    version="1.0.0",
    description="Repository resolution and analysis API",
    lifespan=lifespan,
)
main.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

main.include_router(analyses_router)


def _check_database(factory) -> None:
    conn = factory()
    try:
        conn.fetch_scalar("SELECT 1")
    finally:
        conn.close()


@main.get("/api/health")
async def health() -> dict:
    """Report whether analyses can be created (database) and processed (worker)."""
    factory = getattr(main.state, "connection_factory", None)
    if factory is None:
        database = "unconfigured"
    else:
        try:
            await run_in_threadpool(_check_database, factory)
            database = "ok"
        except Exception:
            logger.warning("Health check could not reach PostgreSQL", exc_info=True)
            database = "unavailable"

    task = getattr(main.state, "worker_task", None)
    if factory is None:
        worker = "unconfigured"
    elif task is None:
        worker = "disabled"
    else:
        worker = "running" if not task.done() else "stopped"

    return {
        "status": "ok" if database == "ok" and worker == "running" else "degraded",
        "database": database,
        "worker": worker,
    }


# Also expose the conventional name for `uvicorn app:app`.
app = main
