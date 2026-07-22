"""
backend/main.py

FastAPI application entry point.
Python equivalent of StackSnifferApplication.java in stacksniffer-api.

Router registration mirrors Spring Boot @RestController component scanning,
but explicit rather than automatic — a FastAPI requirement.
"""
from contextlib import asynccontextmanager
from os import getenv

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.routers import analyze        # POST /api/analyze
from backend.routers import chat           # POST /api/chat/
from backend.routers import feedback       # POST /api/feedback/{id}       (domain RLHF)
from backend.routers import stack_feedback # POST /api/stack-feedback/{id} (tech RLHF)
from backend.routers import learning       # GET  /api/learning/stats
from backend.routers import taxonomy       # POST /api/taxonomy/discover
from backend.routers import insights_feedback #GET /api/insights-feedback/stats          → 404
#GET /api/insights-feedback/quality-criteria → 404
# Remove the try/except guard — replace with explicit import:
from backend.routers import discovery

from backend.services import storage_service
from backend.routers import dep_categories

load_dotenv()



@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup/shutdown lifecycle.
    Java equivalent: ApplicationRunner + @PreDestroy in StackSnifferApplication.java
    """
    await storage_service.init_db()
    await storage_service.seed_builtin_categories()
    yield
    await storage_service.close_db()


app = FastAPI(
    title="StackSniffer API",
    version="1.0.0",
    description="AI-powered tech stack detection and domain analysis engine",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Update allow_origins with your Vercel URL before deploying
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        # "https://your-app.vercel.app",  # uncomment after deploy
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Router registration ───────────────────────────────────────────────────────
# Java equivalent: @RestController auto-detection via @SpringBootApplication
# Python requires explicit include_router() calls — 404 means router not registered
app.include_router(dep_categories.router)
app.include_router(discovery.router)
app.include_router(analyze.router, prefix="/api")
app.include_router(chat.router)
app.include_router(feedback.router)
app.include_router(stack_feedback.router)
app.include_router(learning.router)
app.include_router(taxonomy.router)
app.include_router(insights_feedback.router)

# Discovery router — add when stack_discovery_service.py is in place
try:
    from backend.routers import discovery
    app.include_router(discovery.router)
except ImportError:
    pass  # discovery router not yet created — safe to skip


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
        from pathlib import Path
        classifier_active = Path("backend/models/domain_classifier.pkl").exists()
    except Exception:
        pass

    return {
        "status":             "ok",
        "version":            "1.0.0",
        "ai_enabled":         bool(getenv("GEMINI_API_KEY")),
        "ai_provider":        "gemini",
        "ai_model":           getenv("GEMINI_ANALYSIS_MODEL", "gemini-2.5-flash"),
        "storage":            stats.get("storage", "memory"),
        "total_analyses":     stats.get("total_analyses", 0),
        "with_embeddings":    stats.get("with_embeddings", 0),
        "with_feedback":      stats.get("with_feedback", 0),
        "embedding_coverage": stats.get("embedding_coverage", "0%"),
        "by_domain":          stats.get("by_domain", {}),
        "stack_feedback":     stack_fb,
        "classifier_active":  classifier_active,
        "rag_active":         stats.get("with_embeddings", 0) >= 5,
    }
