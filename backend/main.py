"""
backend/main.py

FastAPI application entry point.
Python equivalent of StackSnifferApplication.java in stacksniffer-api.

Router registration mirrors Spring Boot @RestController component scanning,
but explicit rather than automatic — a FastAPI requirement.
"""
import json
import logging
import logging.config
from contextlib import asynccontextmanager
from os import getenv
from pathlib import Path
from tempfile import gettempdir

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

#GET /api/insights-feedback/quality-criteria → 404
# Remove the try/except guard — replace with explicit import:
from routers import (
    analyze,  # POST /api/analyze
    chat,  # POST /api/chat/
    discovery,
    feedback,  # POST /api/feedback/{id}       (software_type RLHF)
    insights_feedback,  #GET /api/insights-feedback/stats          → 404
    learning,  # GET  /api/learning/stats
    review,
    stack_feedback,  # POST /api/stack-feedback/{id} (tech RLHF)
    taxonomy,  # POST /api/taxonomy/discover
)
from services import storage_service

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_CONFIG_PATH = ROOT_DIR / "logging_config.json"


def _log_path() -> Path:
    """Keep runtime output outside the source tree watched by uvicorn reload."""
    configured_path = getenv("STACKSNIFFER_LOG_PATH")
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return Path(gettempdir()) / "stacksniffer" / "server_output.log"


class _SafeAsciiFormatter(logging.Formatter):
    """Avoid UnicodeEncodeError on Windows consoles (cp1252) by ASCII-fallback logs."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        # Keep logs readable even when Unicode glyphs slip into source strings.
        return (
            msg.encode("ascii", errors="backslashreplace").decode("ascii")
        )


def _configure_logging() -> None:
    try:
        if LOG_CONFIG_PATH.exists():
            with LOG_CONFIG_PATH.open("r", encoding="utf-8") as fp:
                config = json.load(fp)
            file_handler = config.get("handlers", {}).get("file")
            if isinstance(file_handler, dict):
                log_path = _log_path()
                log_path.parent.mkdir(parents=True, exist_ok=True)
                file_handler["filename"] = str(log_path)
            logging.config.dictConfig(config)
    except Exception:
        # Safe fallback; app startup should never be blocked by logging config.
        pass


_configure_logging()
load_dotenv(ROOT_DIR / ".env")

def _ensure_file_logging() -> None:
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler_name = "stacksniffer-file"
    loggers = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("backend"),
    ]
    formatter = _SafeAsciiFormatter(
        "%(asctime)s - %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    for target_logger in loggers:
        already_attached = any(
            getattr(handler, "name", "") == file_handler_name
            or (
                isinstance(handler, logging.FileHandler)
                and Path(handler.baseFilename).resolve() == log_path.resolve()
            )
            for handler in target_logger.handlers
        )
        if already_attached:
            continue
        file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.set_name(file_handler_name)
        file_handler.setFormatter(formatter)
        target_logger.addHandler(file_handler)
        target_logger.setLevel(logging.INFO)


def _allowed_origins() -> list[str]:
    configured = getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:3000",
    )
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup/shutdown lifecycle.
    Java equivalent: ApplicationRunner + @PreDestroy in StackSnifferApplication.java
    """
    _ensure_file_logging()
    await storage_service.init_db()
    await storage_service.seed_builtin_technology_roles()
    yield
    await storage_service.close_db()


app = FastAPI(
    title="StackSniffer API",
    version="1.0.0",
    description="AI-powered tech stack detection and software_type analysis engine",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Update allow_origins with your Vercel URL before deploying
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Router registration ───────────────────────────────────────────────────────
# Java equivalent: @RestController auto-detection via @SpringBootApplication
# Python requires explicit include_router() calls — 404 means router not registered
app.include_router(discovery.router)
app.include_router(analyze.router, prefix="/api")
app.include_router(chat.router)
app.include_router(feedback.router)
app.include_router(stack_feedback.router)
app.include_router(learning.router)
app.include_router(taxonomy.router)
app.include_router(insights_feedback.router)
app.include_router(review.router)

# ── Health endpoint ───────────────────────────────────────────────────────────
# Java equivalent: HealthController.java
@app.get("/api/health")
async def health():
    stats = await storage_service.get_stats()

    stack_fb = {}
    try:
        stack_fb = await storage_service.get_stack_feedback_stats()
    except Exception:
        pass

    classifier_active = False
    try:
        from services.learning_service import load_layer0
        classifier_active = load_layer0() is not None
    except Exception:
        pass

    return {
        "status":             "ok",
        "version":            "1.0.0",
        "pipeline_version":   storage_service.PIPELINE_VERSION,
        "ai_enabled":         bool(getenv("GEMINI_API_KEY")),
        "ai_provider":        "gemini",
        "ai_model":           getenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash"),
        "storage":            stats.get("storage", "memory"),
        "total_analyses":     stats.get("total_analyses", 0),
        "with_embeddings":    stats.get("with_embeddings", 0),
        "with_feedback":      stats.get("with_feedback", 0),
        "embedding_coverage": stats.get("embedding_coverage", "0%"),
        "by_software_type":          stats.get("by_software_type", {}),
        "stack_feedback":     stack_fb,
        "classifier_active":  classifier_active,
        "rag_active":         stats.get("with_embeddings", 0) >= 5,
    }

