"""
backend/services/storage_service.py

Python equivalent of:
  stacksniffer-search/ElasticsearchServiceImpl.java    → analysis CRUD
  stacksniffer-search/HybridSearchServiceImpl.java     → find_similar() kNN
  stacksniffer-learning/UsageTrackingRepository.java   → feedback storage

Imported as storage_service throughout routers to match existing filename.
Both names refer to this file — rename the file when ready.

Key design:
  - In-memory fallback when MONGODB_URI not set (dev mode)
  - Atlas $vectorSearch for kNN similarity (requires stack_vector_index)
  - store_stack_feedback() normalises quick-feedback and batch-feedback
    into consistent tech_evaluations format for compute_per_tech_accuracy()
  - Domain taxonomy stored in domains collection (emergent taxonomy)
"""
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from os import getenv
from datetime import datetime, timedelta
from dotenv import load_dotenv
import logging

load_dotenv()
logger = logging.getLogger(__name__)

_client = None
_db     = None
_memory_store:         dict = {}
_feedback_store:       dict = {}
_stack_feedback_store: dict = {}
_domain_store:         dict = {}


async def init_db() -> None:
    global _client, _db
    uri = getenv("MONGODB_URI")
    if not uri:
        logger.warning("MONGODB_URI not set — using in-memory fallback")
        print("MONGODB_URI not set — using in-memory fallback")
        return

    _client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
    _db = _client[getenv("MONGODB_DB", "stacksniffer")]

    await _db.analyses.create_index("analysis_id", unique=True)
    await _db.analyses.create_index("repo.full_name")
    await _db.analyses.create_index("stack.domain")
    await _db.analyses.create_index(
        "created_at", expireAfterSeconds=604800   # 7-day TTL
    )
    await _db.analyses.create_index("stack_embedding", sparse=True)
    await _db.feedback.create_index("analysis_id")
    await _db.feedback.create_index("created_at")
    await _db.stack_feedback.create_index("analysis_id")
    await _db.stack_feedback.create_index("created_at")
    await _db.domains.create_index("domain_id", unique=True)

    print(f"MongoDB connected: {getenv('MONGODB_DB', 'stacksniffer')}")
    logger.info("MongoDB connected")


async def close_db() -> None:
    if _client:
        _client.close()


def is_available() -> bool:
    return _db is not None


# ── Analysis CRUD ─────────────────────────────────────────────────────────────

async def store_analysis(analysis_id: str, result: dict) -> None:
    doc = {**result, "analysis_id": analysis_id, "created_at": datetime.utcnow()}
    if _db is not None:
        await _db.analyses.update_one(
            {"analysis_id": analysis_id}, {"$set": doc}, upsert=True
        )
    else:
        _memory_store[analysis_id] = doc


async def store_analysis_with_embedding(
    analysis_id: str,
    result: dict,
    embedding: list[float]
) -> None:
    doc = {
        **result,
        "analysis_id":    analysis_id,
        "created_at":     datetime.utcnow(),
        "stack_embedding": embedding,
        "embedding_model": "models/gemini-embedding-001",
        "embedding_dim":   len(embedding),
    }
    if _db is not None:
        await _db.analyses.update_one(
            {"analysis_id": analysis_id}, {"$set": doc}, upsert=True
        )
        logger.info("Stored %s with %d-dim embedding", analysis_id, len(embedding))
    else:
        _memory_store[analysis_id] = doc


async def get_analysis(analysis_id: str) -> dict | None:
    if _db is not None:
        doc = await _db.analyses.find_one({"analysis_id": analysis_id}, {"_id": 0})
        return await _apply_insights_corrections(doc)
    return await _apply_insights_corrections(_memory_store.get(analysis_id))


async def find_cached_repo(full_name: str, max_age_hours: int = 1) -> dict | None:
    if _db is None:
        return None
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    doc = await _db.analyses.find_one(
        {"repo.full_name": full_name, "created_at": {"$gte": cutoff}},
        {"_id": 0},
        sort=[("created_at", DESCENDING)]
    )
    return await _apply_insights_corrections(doc)


async def find_latest_by_repo(full_name: str) -> dict | None:
    if _db is None:
        return None
    doc = await _db.analyses.find_one(
        {"repo.full_name": full_name},
        {"_id": 0},
        sort=[("created_at", DESCENDING)]
    )
    return await _apply_insights_corrections(doc)


async def count_embedded_analyses() -> int:
    """
    RAG corpus size check.
    Used by ai_pipeline to gate retrieval — skip if corpus < 5.
    Java equivalent: ElasticsearchServiceImpl.countDocuments() on code_chunks index.
    """
    if _db is None:
        return sum(1 for v in _memory_store.values() if v.get("stack_embedding"))
    return await _db.analyses.count_documents(
        {"stack_embedding": {"$exists": True, "$not": {"$size": 0}}}
    )


# ── Vector similarity search — HybridSearchServiceImpl.findSimilar() ──────────

async def find_similar(embedding: list[float], limit: int = 5) -> list[dict]:
    """
    kNN vector search via Atlas $vectorSearch.
    Requires Atlas Vector Search index 'stack_vector_index' on stack_embedding.

    Python limitation vs Java: kNN only.
    Java HybridSearchServiceImpl combines BM25 + kNN with score fusion.
    BM25 component not implemented in Python demo.
    """
    if _db is None:
        return []
    try:
        pipeline = [
            {
                "$vectorSearch": {
                    "index":        "stack_vector_index",
                    "path":         "stack_embedding",
                    "queryVector":  embedding,
                    "numCandidates": limit * 10,
                    "limit":        limit,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "analysis_id":            1,
                    "repo.full_name":         1,
                    "repo.description":       1,
                    "repo.stars":             1,
                    "stack.domain":           1,
                    "stack.domain_confidence": 1,
                    "stack.domain_reasoning": 1,
                    "stack.stack_pattern":    1,
                    "stack.why_this_stack":   1,
                    "stack.primary_language": 1,
                    "stack.architecture_style": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            }
        ]
        cursor  = _db.analyses.aggregate(pipeline)
        results = await cursor.to_list(limit)
        results = await _apply_insights_corrections_to_many(results)
        logger.info("Vector search returned %d results", len(results))
        return results
    except Exception as e:
        logger.error("Vector search failed: %s", str(e)[:300])
        return []


async def find_similar_by_domain(domain: str, limit: int = 3) -> list[dict]:
    """Domain-based fallback when no embeddings available."""
    if _db is None:
        return []
    cursor = _db.analyses.find(
        {"stack.domain": domain, "stack_embedding": {"$exists": True}},
        {
            "_id": 0,
            "analysis_id":            1,
            "repo.full_name":         1,
            "stack.domain":           1,
            "stack.domain_reasoning": 1,
            "stack.stack_pattern":    1,
            "stack.why_this_stack":   1,
        }
    ).sort("created_at", DESCENDING).limit(limit)
    results = await cursor.to_list(limit)
    return await _apply_insights_corrections_to_many(results)


# ── Domain-level feedback — UsageTrackingRepository ──────────────────────────

async def store_feedback(analysis_id: str, feedback: dict) -> None:
    doc = {**feedback, "analysis_id": analysis_id, "created_at": datetime.utcnow()}
    if _db is not None:
        await _db.feedback.insert_one(doc)
        if not feedback.get("domain_correct") and feedback.get("correct_domain"):
            await _db.analyses.update_one(
                {"analysis_id": analysis_id},
                {"$set": {
                    "stack.domain": feedback["correct_domain"],
                    "domain_corrected": True,
                }},
            )
    else:
        _feedback_store[analysis_id] = doc
        if not feedback.get("domain_correct") and feedback.get("correct_domain"):
            analysis = _memory_store.get(analysis_id)
            if analysis:
                analysis.setdefault("stack", {})["domain"] = feedback["correct_domain"]
                analysis["domain_corrected"] = True


async def get_all_feedback() -> list[dict]:
    if _db is None:
        return list(_feedback_store.values())
    cursor = _db.feedback.find({}, {"_id": 0})
    return await cursor.to_list(10000)


async def get_feedback_for_analysis(analysis_id: str) -> dict | None:
    if _db is None:
        return _feedback_store.get(analysis_id)
    return await _db.feedback.find_one({"analysis_id": analysis_id}, {"_id": 0})


# ── Stack-level feedback — PatternValidationServiceImpl ──────────────────────

async def store_stack_feedback(analysis_id: str, evaluation: dict) -> None:
    """
    Normalise feedback shape before storage.
    Two call patterns exist in stack_feedback.py:
      A. Batch: evaluation already contains tech_evaluations list
      B. Quick: evaluation is a flat {tech_name, verdict, reason, category} dict

    Both are normalised to {tech_evaluations: [...]} for consistent
    retrieval by compute_per_tech_accuracy().
    """
    # If already a batch-shaped dict pass through; otherwise wrap
    if "tech_evaluations" in evaluation:
        doc = {**evaluation, "analysis_id": analysis_id, "created_at": datetime.utcnow()}
    else:
        # Quick feedback — wrap flat dict into tech_evaluations list
        doc = {
            "analysis_id":     analysis_id,
            "created_at":      datetime.utcnow(),
            "tech_evaluations": [evaluation],
            "quick_feedback":  evaluation.get("quick_feedback", True),
        }

    if _db is not None:
        await _db.stack_feedback.insert_one(doc)
    else:
        _stack_feedback_store.setdefault(analysis_id, []).append(doc)


async def get_all_stack_feedback() -> list[dict]:
    if _db is None:
        return [
            item
            for entries in _stack_feedback_store.values()
            for item in entries
        ]
    cursor = _db.stack_feedback.find({}, {"_id": 0})
    return await cursor.to_list(10000)


async def get_stack_feedback_stats() -> dict:
    if _db is None:
        total = sum(len(v) for v in _stack_feedback_store.values())
        return {"total": total, "storage": "memory"}
    total = await _db.stack_feedback.count_documents({})
    return {"total_stack_feedback_submissions": total, "storage": "mongodb"}


# ── Training data — FrequentPatternMiner input ────────────────────────────────

async def get_labeled_training_data() -> list[dict]:
    """
    Analyses joined with feedback — used for classifier training.
    Java equivalent: UsageTrackingRepository aggregation + FrequentPatternMiner.
    """
    if _db is None:
        return []
    pipeline = [
        {
            "$lookup": {
                "from":         "feedback",
                "localField":   "analysis_id",
                "foreignField": "analysis_id",
                "as":           "feedback",
            }
        },
        {"$match": {"feedback": {"$ne": []}}},
        {"$unwind": "$feedback"},
        {
            "$project": {
                "_id": 0,
                "analysis_id":       1,
                "repo_name":         "$repo.full_name",
                "detected_domain":   "$stack.domain",
                "correct_domain": {
                    "$cond": {
                        "if":   "$feedback.domain_correct",
                        "then": "$stack.domain",
                        "else": "$feedback.correct_domain",
                    }
                },
                "domain_correct":    "$feedback.domain_correct",
                "stack_embedding":   1,
                "primary_language":  "$stack.primary_language",
                "complexity_score":  "$stack.complexity_score",
                "ai_calls_made":     "$stack.ai_calls_made",
                "patterns_checked":  "$stack.patterns_checked",
            }
        }
    ]
    cursor = _db.analyses.aggregate(pipeline)
    return await cursor.to_list(10000)


async def get_all_analyses(with_embeddings_only: bool = False) -> list[dict]:
    if _db is None:
        analyses = [
            value for key, value in _memory_store.items()
            if key != "insights_feedback"
        ]
        return await _apply_insights_corrections_to_many(analyses)
    query  = {"stack_embedding": {"$exists": True}} if with_embeddings_only else {}
    cursor = _db.analyses.find(query, {"_id": 0})
    analyses = await cursor.to_list(10000)
    return await _apply_insights_corrections_to_many(analyses)


# ── Query helpers ─────────────────────────────────────────────────────────────

async def find_by_domain(domain: str, limit: int = 20) -> list[dict]:
    if _db is None:
        return []
    cursor = _db.analyses.find(
        {"stack.domain": domain},
        {
            "_id": 0,
            "analysis_id":          1,
            "repo.full_name":       1,
            "repo.description":     1,
            "stack.why_this_stack": 1,
            "stack.stack_pattern":  1,
            "stack.primary_language": 1,
            "created_at":           1,
        }
    ).sort("created_at", DESCENDING).limit(limit)
    results = await cursor.to_list(limit)
    return await _apply_insights_corrections_to_many(results)


async def find_by_stack_pattern(pattern: str, limit: int = 10) -> list[dict]:
    if _db is None:
        return []
    cursor = _db.analyses.find(
        {"stack.stack_pattern": {"$regex": pattern, "$options": "i"}},
        {"_id": 0, "analysis_id": 1, "repo.full_name": 1, "stack.stack_pattern": 1}
    ).sort("created_at", DESCENDING).limit(limit)
    return await cursor.to_list(limit)


async def get_stats() -> dict:
    if _db is None:
        return {
            "storage":         "memory",
            "total_analyses":  len(_memory_store),
            "with_embeddings": sum(1 for v in _memory_store.values() if v.get("stack_embedding")),
            "with_feedback":   len(_feedback_store),
        }
    total           = await _db.analyses.count_documents({})
    with_embeddings = await _db.analyses.count_documents({"stack_embedding": {"$exists": True}})
    with_feedback   = await _db.feedback.count_documents({})
    by_domain       = await _db.analyses.aggregate([
        {"$group": {"_id": "$stack.domain", "count": {"$sum": 1}}},
        {"$sort":  {"count": DESCENDING}},
    ]).to_list(20)
    return {
        "storage":           "mongodb",
        "total_analyses":    total,
        "with_embeddings":   with_embeddings,
        "with_feedback":     with_feedback,
        "embedding_coverage": f"{with_embeddings / max(total, 1) * 100:.0f}%",
        "by_domain":         {d["_id"]: d["count"] for d in by_domain if d["_id"]},
    }


# ── Domain taxonomy — DynamicPatternConfigService equivalent ──────────────────

async def store_domain(domain_id: str, doc: dict) -> None:
    if _db is not None:
        await _db.domains.update_one(
            {"domain_id": domain_id}, {"$set": doc}, upsert=True
        )
    else:
        _domain_store[f"domain:{domain_id}"] = doc


async def get_all_domains() -> list[dict]:
    if _db is not None:
        cursor = _db.domains.find(
            {"status": {"$ne": "deleted"}}, {"_id": 0}
        ).sort("usage_count", DESCENDING)
        return await cursor.to_list(100)
    return [
        v for k, v in _domain_store.items()
        if k.startswith("domain:") and v.get("status") != "deleted"
    ]


async def delete_domain(domain_id: str) -> None:
    if _db is not None:
        await _db.domains.update_one(
            {"domain_id": domain_id},
            {"$set": {"status": "deleted", "deleted_at": datetime.utcnow()}}
        )
    else:
        key = f"domain:{domain_id}"
        if key in _domain_store:
            _domain_store[key]["status"] = "deleted"


async def increment_domain_usage(domain_id: str) -> None:
    if _db is not None:
        await _db.domains.update_one(
            {"domain_id": domain_id}, {"$inc": {"usage_count": 1}}, upsert=False
        )


async def store_insights_feedback(analysis_id: str, doc: dict) -> None:
    if _db is not None:
        await _db.insights_feedback.insert_one({**doc})
        await _db.insights_feedback.create_index("analysis_id")
    else:
        _memory_store.setdefault("insights_feedback", []).append(doc)


async def update_analysis_insights(
    analysis_id: str,
    replacement_text: dict,
) -> bool:
    """
    Overwrite corrected AI insight fields on the stored analysis document.

    Insight feedback is a gold label, not just an annotation. Keeping the
    analysis cache in sync ensures similar-repo panels and RAG corpus context
    use the user's corrected architectural fields.
    """
    if not replacement_text:
        return False

    field_map = {
        "why_this_stack":       "stack.why_this_stack",
        "stack_pattern":        "stack.stack_pattern",
        "ecosystem_context":    "stack.ecosystem_context",
        "notable_combinations": "stack.notable_combinations",
    }
    update_fields = {
        field_map[field]: _normalize_insight_value(field, value)
        for field, value in replacement_text.items()
        if field in field_map
    }
    if not update_fields:
        return False

    if _db is not None:
        result = await _db.analyses.update_one(
            {"analysis_id": analysis_id},
            {"$set": {**update_fields, "insights_corrected": True}},
        )
        return result.modified_count > 0 or result.matched_count > 0

    doc = _memory_store.get(analysis_id)
    if not doc:
        return False
    stack = doc.setdefault("stack", {})
    for field, value in replacement_text.items():
        if field in field_map:
            stack[field] = _normalize_insight_value(field, value)
    doc["insights_corrected"] = True
    return True


def _normalize_insight_value(field: str, value):
    if field != "notable_combinations" or not isinstance(value, str):
        return value
    return [
        item.strip()
        for item in value.splitlines()
        if item.strip()
    ]


async def _apply_insights_corrections(doc: dict | None) -> dict | None:
    if not doc:
        return doc
    corrected = await _latest_insights_corrections([doc.get("analysis_id")])
    replacement_text = corrected.get(doc.get("analysis_id"))
    if replacement_text:
        _apply_replacement_text(doc, replacement_text)
    return doc


async def _apply_insights_corrections_to_many(docs: list[dict]) -> list[dict]:
    if not docs:
        return docs
    corrected = await _latest_insights_corrections([
        doc.get("analysis_id") for doc in docs if doc.get("analysis_id")
    ])
    for doc in docs:
        replacement_text = corrected.get(doc.get("analysis_id"))
        if replacement_text:
            _apply_replacement_text(doc, replacement_text)
    return docs


async def _latest_insights_corrections(analysis_ids: list[str | None]) -> dict[str, dict]:
    ids = [analysis_id for analysis_id in analysis_ids if analysis_id]
    if not ids:
        return {}

    latest: dict[str, dict] = {}
    if _db is not None:
        cursor = _db.insights_feedback.find(
            {
                "analysis_id": {"$in": ids},
                "replacement_text": {"$exists": True, "$ne": None},
            },
            {"_id": 0, "analysis_id": 1, "replacement_text": 1, "created_at": 1},
        ).sort("created_at", ASCENDING)
        feedback = await cursor.to_list(1000)
    else:
        feedback = [
            item for item in _memory_store.get("insights_feedback", [])
            if item.get("analysis_id") in ids and item.get("replacement_text")
        ]

    for item in feedback:
        replacement_text = item.get("replacement_text")
        if isinstance(replacement_text, dict):
            latest[item["analysis_id"]] = replacement_text
    return latest


def _apply_replacement_text(doc: dict, replacement_text: dict) -> None:
    stack = doc.setdefault("stack", {})
    for field, value in replacement_text.items():
        if field in {
            "why_this_stack",
            "stack_pattern",
            "ecosystem_context",
            "notable_combinations",
        }:
            stack[field] = _normalize_insight_value(field, value)
    doc["insights_corrected"] = True


async def get_all_insights_feedback() -> list[dict]:
    if _db is not None:
        cursor = _db.insights_feedback.find({}, {"_id": 0})
        return await cursor.to_list(10000)
    return _memory_store.get("insights_feedback", [])

async def get_quality_criteria() -> list[dict]:
    if _db is not None:
        cursor = _db.quality_criteria.find(
            {"field": {"$ne": "_overall"}}, {"_id": 0}
        ).sort("field", 1)
        return await cursor.to_list(20)
    return []

async def update_quality_criterion(field: str, criterion: dict) -> None:
    if _db is not None:
        await _db.quality_criteria.update_one(
            {"field": field},
            {"$set": {**criterion, "updated_at": datetime.utcnow()}},
            upsert=True
        )
