"""ASGI entry point for repository and analysis APIs.

Run with ``uvicorn main:app`` from backend/ or ``uvicorn backend.main:app`` from
the repository root. When DATABASE_URL is set, the same process also runs the
analysis worker that moves QUEUED analyses through the pipeline.

``create_app`` loads backend/.env before building the app. Setting
STACKSNIFFER_LOAD_DOTENV=0 skips that; the test suite does so (see conftest.py)
so importing this module never exposes live credentials to tests.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    from cors_config import allowed_origins
    from routers.analyses import configure_connection_factory
    from routers.analyses import router as analyses_router
    from services.analysis_pipeline import AnalysisPipeline
    from services.analysis_worker import supervise_worker
    from services.extraction.registry import AnalyzerRegistry
    from services.extraction.stage import StructuralExtractionStage
    from services.postgres import PostgresUnavailable, make_connection_factory
    from services.repository_acquisition import AcquisitionLimits, GitRepositoryAcquirer
    from services.repository_resolver import github_reported_size_bytes
except ModuleNotFoundError:
    from backend.cors_config import allowed_origins
    from backend.routers.analyses import configure_connection_factory
    from backend.routers.analyses import router as analyses_router
    from backend.services.analysis_pipeline import AnalysisPipeline
    from backend.services.analysis_worker import supervise_worker
    from backend.services.extraction.registry import AnalyzerRegistry
    from backend.services.extraction.stage import StructuralExtractionStage
    from backend.services.postgres import PostgresUnavailable, make_connection_factory
    from backend.services.repository_acquisition import (
        AcquisitionLimits,
        GitRepositoryAcquirer,
    )
    from backend.services.repository_resolver import github_reported_size_bytes

logger = logging.getLogger(__name__)

WORKER_SHUTDOWN_SECONDS = 10.0
# Languages whose structural analyzer must be available for the service to be ready (R1: Java,
# Python). SS-BE-203 adds scala, go, javascript and ruby here as their analyzers ship.
DEFAULT_REQUIRED_LANGUAGES = "python,java"


def load_environment() -> bool:
    """Load backend/.env unless STACKSNIFFER_LOAD_DOTENV disables it; returns whether it was loaded."""
    if not _env_flag("STACKSNIFFER_LOAD_DOTENV", True):
        return False
    return load_dotenv(Path(__file__).resolve().with_name(".env"))


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _worker_options() -> dict:
    return {
        "poll_seconds": float(os.getenv("ANALYSIS_WORKER_POLL_SECONDS", "1.0")),
        "stale_attempt_seconds": float(os.getenv("ANALYSIS_ATTEMPT_STALE_SECONDS", "1800")),
        "max_attempts": int(os.getenv("ANALYSIS_MAX_ATTEMPTS", "3")),
    }


def _acquirer() -> GitRepositoryAcquirer:
    defaults = AcquisitionLimits()
    limits = AcquisitionLimits(
        timeout_seconds=float(os.getenv("ANALYSIS_ACQUISITION_TIMEOUT_SECONDS", defaults.timeout_seconds)),
        max_checkout_bytes=int(os.getenv("ANALYSIS_MAX_CHECKOUT_BYTES", defaults.max_checkout_bytes)),
        max_file_count=int(os.getenv("ANALYSIS_MAX_FILE_COUNT", defaults.max_file_count)),
        max_file_bytes=int(os.getenv("ANALYSIS_MAX_FILE_BYTES", defaults.max_file_bytes)),
        max_workdir_bytes=int(os.getenv("ANALYSIS_MAX_WORKDIR_BYTES", defaults.max_workdir_bytes)),
    )
    return GitRepositoryAcquirer(
        limits,
        workdir_root=os.getenv("ANALYSIS_WORKDIR") or None,
        size_probe=github_reported_size_bytes,
    )


async def _remove_orphaned_acquisitions(acquirer: GitRepositoryAcquirer) -> None:
    """Sweep checkouts left by a crashed process. The TTL must outlive any live attempt."""
    ttl = float(os.getenv("ANALYSIS_ACQUISITION_ORPHAN_TTL_SECONDS", "7200"))
    try:
        removed = await asyncio.to_thread(acquirer.remove_stale_acquisitions, ttl)
    except Exception:
        logger.exception("Orphaned acquisition cleanup failed; continuing startup")
        return
    if removed:
        logger.info("Removed %d orphaned acquisition directories", len(removed))


def _required_languages() -> list[str]:
    value = os.getenv("ANALYSIS_REQUIRED_LANGUAGES", DEFAULT_REQUIRED_LANGUAGES)
    return sorted({language.strip().lower() for language in value.split(",") if language.strip()})


def unavailable_languages(registry: AnalyzerRegistry, required: list[str]) -> dict[str, str]:
    """Required languages that would silently fall back to file-level analysis, with the reason."""
    analyzers = {analyzer.language: analyzer for analyzer in registry.analyzers}
    missing = {}
    for language in required:
        analyzer = analyzers.get(language)
        if analyzer is None:
            missing[language] = "no analyzer registered"
        elif not analyzer.available:
            reason = getattr(getattr(analyzer, "adapter", None), "unavailable_reason", None)
            missing[language] = f"grammar unavailable: {reason}" if reason else "grammar unavailable"
    return missing


def _structural_stage(connection_factory, registry: AnalyzerRegistry) -> StructuralExtractionStage:
    for analyzer in registry.analyzers:
        if analyzer.available:
            logger.info("Structural analyzer for %s: %s", analyzer.language, analyzer.extractor_version)
        else:
            logger.error("No Tree-sitter grammar for %s; /api/health/ready will fail if it is required",
                         analyzer.language)
    return StructuralExtractionStage(
        connection_factory,
        registry,
        batch_size=int(os.getenv("ANALYSIS_EXTRACTION_BATCH_FILES", "100")),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Configure PostgreSQL persistence and run the analysis worker alongside the API."""
    app.state.connection_factory = None
    app.state.worker_task = None
    app.state.analyzer_registry = AnalyzerRegistry.default()
    app.state.required_languages = _required_languages()
    if missing := unavailable_languages(app.state.analyzer_registry, app.state.required_languages):
        logger.error("Required structural analyzers are unavailable: %s", missing)
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
            acquirer = _acquirer()
            await _remove_orphaned_acquisitions(acquirer)
            app.state.worker_task = asyncio.create_task(
                supervise_worker(
                    app.state.connection_factory,
                    AnalysisPipeline(
                        app.state.connection_factory,
                        acquirer,
                        structural_stage=_structural_stage(app.state.connection_factory, app.state.analyzer_registry),
                    ),
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


def _check_database(factory) -> None:
    conn = factory()
    try:
        conn.fetch_scalar("SELECT 1")
    finally:
        conn.close()


async def _readiness(state) -> dict:
    factory = getattr(state, "connection_factory", None)
    if factory is None:
        database = "unconfigured"
    else:
        try:
            await run_in_threadpool(_check_database, factory)
            database = "ok"
        except Exception:
            logger.warning("Health check could not reach PostgreSQL", exc_info=True)
            database = "unavailable"

    task = getattr(state, "worker_task", None)
    if factory is None:
        worker = "unconfigured"
    elif task is None:
        worker = "disabled"
    else:
        worker = "running" if not task.done() else "stopped"

    registry = getattr(state, "analyzer_registry", None) or AnalyzerRegistry.default()
    required = getattr(state, "required_languages", None) or _required_languages()
    missing = unavailable_languages(registry, required)
    worker_ok = worker == "running" or (worker == "disabled" and not _env_flag("ANALYSIS_WORKER_ENABLED", True))
    failures = [name for name, ok in (("database", database == "ok"), ("worker", worker_ok),
                                      ("analyzers", not missing)) if not ok]
    return {
        "status": "ok" if not failures else "degraded",
        "database": database,
        "worker": worker,
        "analyzers": {"required": required, "unavailable": missing},
        "failing": failures,
    }


async def health(request: Request) -> dict:
    """Summary for the UI: always 200; ``status`` is "ok" only when the service is ready."""
    readiness = await _readiness(request.app.state)
    return {key: readiness[key] for key in ("status", "database", "worker", "analyzers")}


async def health_live() -> dict:
    """Liveness: the process is up and serving requests."""
    return {"status": "ok"}


async def health_ready(request: Request) -> JSONResponse:
    """Readiness: 503 unless the database, worker and every required analyzer are available."""
    readiness = await _readiness(request.app.state)
    return JSONResponse(readiness, status_code=200 if not readiness["failing"] else 503)


def create_app() -> FastAPI:
    """Load configuration, then build the API. Configuration is read here, not at import."""
    load_environment()
    # No-op when the host already configured logging; otherwise worker progress would be invisible.
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    application = FastAPI(
        title="StackSniffer API",
        version="1.0.0",
        description="Repository resolution and analysis API",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(analyses_router)
    application.add_api_route("/api/health", health, methods=["GET"])
    application.add_api_route("/api/health/live", health_live, methods=["GET"])
    application.add_api_route("/api/health/ready", health_ready, methods=["GET"])
    return application


main = create_app()

# Also expose the conventional name for `uvicorn app:app`.
app = main
