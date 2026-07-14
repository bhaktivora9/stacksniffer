"""
backend/services/embedding_service.py
Generates vector embeddings for stack fingerprints.
Used by RAG retrieval and similarity search.

Migration notes (text-embedding-004 -> gemini-embedding-001):
  1. text-embedding-004 was shut down 2026-01-14. Model string is now
     models/gemini-embedding-001.
  2. gemini-embedding-001 returns 3072-dim vectors natively (was 768).
     EMBEDDING_DIM and the Atlas index numDimensions must both be 3072.
  3. asyncio.to_thread must receive a CALLABLE + args, not the result of
     calling embed_content. Passing the dict result made the network call
     run on the event loop and then raised "'dict' object is not callable".
  4. task_type must differ by role: retrieval_document on the store side,
     retrieval_query on the probe side. embed_stack plays both roles, so
     the caller selects the task_type.
"""

import asyncio
import logging
from os import getenv

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Model selection ──────────────────────────────────────────────────────────
# Gemini embeddings (reuses the existing GEMINI_API_KEY, no new dependency).
EMBEDDING_PROVIDER = getenv("EMBEDDING_PROVIDER", "gemini")
EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_DIM = 3072  # gemini-embedding-001 native dimension (was 768 for -004)

genai.configure(api_key=getenv("GEMINI_API_KEY", ""))


def build_stack_fingerprint(stack: dict) -> str:
    """
    Build text fingerprint for embedding.

    Two modes:
      Pre-AI  (domain=unknown): sparse — tech signals only
      Post-AI (domain set):     rich   — includes semantic fields

    Rich fingerprints make RAG retrieval semantically meaningful.
    Repos with similar stacks AND similar architectural intent
    cluster together, not just repos with similar tech lists.
    """
    parts = []

    # ── Domain + architecture (highest semantic signal) ────────────────────
    domain = stack.get("domain", "unknown")
    arch = stack.get("architecture_style", "unknown")
    pattern = stack.get("stack_pattern", "")

    if domain and domain != "unknown":
        parts.append(f"domain:{domain}")
    if arch and arch != "unknown":
        parts.append(f"architecture:{arch}")
    if pattern and pattern not in ("", "Custom"):
        parts.append(f"pattern:{pattern}")

    # ── Technology signals ─────────────────────────────────────────────────
    for category in [
        "languages",
        "frameworks",
        "databases",
        "messaging",
        "ai_ml",
        "infra",
        "testing",
        "library",
    ]:
        techs = stack.get(category, [])
        if techs:
            names = []
            for t in techs:
                if isinstance(t, dict):
                    names.append(t.get("name", ""))
                elif hasattr(t, "name"):
                    names.append(t.name)
                else:
                    names.append(str(t))
            names = [n for n in names if n]
            if names:
                parts.append(f"{category}:{','.join(names)}")

    # ── Semantic fields (only present in post-AI enriched fingerprint) ─────
    why = stack.get("why_this_stack", "")
    if why:
        parts.append(why)

    ecosystem = stack.get("ecosystem_context", "")
    if ecosystem:
        parts.append(ecosystem)

    notable = stack.get("notable_combinations", [])
    if notable and isinstance(notable, list):
        parts.append(" ".join(notable[:2]))  # first 2 only — avoid noise

    # ── Target user (if present) ───────────────────────────────────────────
    target_user = stack.get("target_user", "")
    if target_user:
        parts.append(f"users:{target_user}")

    return " | ".join(p for p in parts if p)


def is_valid_embedding(vec: list[float]) -> bool:
    """
    True only for a usable vector: correct length and non-zero magnitude.

    Callers that PERSIST a vector must guard on this. A zero vector has
    undefined cosine similarity and will poison Atlas retrieval scores if
    stored. The probe (query) side may tolerate a failed embedding — it
    simply returns no useful matches — but the store side must not.
    """
    return len(vec) == EMBEDDING_DIM and any(v != 0.0 for v in vec)


async def embed_text(text: str, task_type: str = "retrieval_document") -> list[float]:
    """
    Generate an embedding vector for a text string.

    task_type: "retrieval_document" for text being stored/indexed,
               "retrieval_query"    for a probe against the corpus.

    Returns a {EMBEDDING_DIM}-dim float list. Falls back to a zero vector
    on failure — callers that store the result must check is_valid_embedding.
    """
    if not text.strip():
        return [0.0] * EMBEDDING_DIM

    try:
        result = await asyncio.to_thread(
            genai.embed_content,          # callable reference — NOT genai.embed_content(...)
            model=EMBEDDING_MODEL,
            content=text,
            task_type=task_type,
        )
        vec = result["embedding"]
        if len(vec) != EMBEDDING_DIM:
            logger.error(
                "Embedding dim mismatch: got %d, expected %d "
                "(check EMBEDDING_DIM vs Atlas numDimensions)",
                len(vec), EMBEDDING_DIM,
            )
        return vec
    except Exception as e:
        logger.error("Embedding failed: %s", str(e)[:200])
        return [0.0] * EMBEDDING_DIM


async def embed_stack(stack: dict, task_type: str = "retrieval_document") -> list[float]:
    """
    Embed a full StackAnalysis dict.

    Defaults to retrieval_document (the store/ingest role). When used as a
    probe against the corpus — e.g. ai_pipeline.run_full_ai_pipeline — pass
    task_type="retrieval_query".
    """
    fingerprint = build_stack_fingerprint(stack)
    logger.info("Embedding fingerprint: %s", fingerprint[:100])
    return await embed_text(fingerprint, task_type=task_type)


async def embed_query(query: str) -> list[float]:
    """Embed a free-text user query for similarity search (probe role)."""
    return await embed_text(query, task_type="retrieval_query")