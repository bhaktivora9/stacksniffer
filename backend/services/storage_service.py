"""
Storage layer for StackSniffer.

Analysis results are keyed by canonical repo key in analyses_result. Request,
feedback, corrections, and event history live outside that mutable result doc.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from os import getenv
from uuid import uuid4
import logging
import time

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from backend.services.repo_key import parse_repo_key

load_dotenv()
logger = logging.getLogger(__name__)

PIPELINE_VERSION = getenv("PIPELINE_VERSION", "dev")
BUILTIN_DOMAINS: tuple[dict, ...] = (
    {"_id": "database", "label": "Database", "builtin": True, "active": True, "order": 1},
    {"_id": "data_pipeline", "label": "Data Pipeline", "builtin": True, "active": True, "order": 2},
    {"_id": "ml_platform", "label": "ML Platform", "builtin": True, "active": True, "order": 3},
    {"_id": "infra_tool", "label": "Infra Tool", "builtin": True, "active": True, "order": 4},
    {"_id": "web_app", "label": "Web App", "builtin": True, "active": True, "order": 5},
    {"_id": "library", "label": "Library", "builtin": True, "active": True, "order": 6},
    {"_id": "unknown", "label": "Unknown", "builtin": True, "active": True, "order": 99, "sentinel": True},
)
BUILTIN_CATEGORIES: tuple[str, ...] = (
    "languages", "frameworks", "databases", "messaging",
    "ai_ml", "infra", "testing", "library",
)
_TAXONOMY_CACHE_TTL = 30.0
_domain_cache: tuple[float, list[dict]] | None = None
_category_cache: tuple[float, list[dict]] | None = None
ALLOWED_CORRECTION_FIELDS = {
    "why_this_stack",
    "stack_pattern",
    "ecosystem_context",
    "notable_combinations",
    "domain",
    "primary_language",
}
_LIST_STORES = {
    "analysis_events",
    "insights_feedback",
    "dep_categories",
    "dep_category_feedback",
}

_client = None
_db = None
_memory_store: dict = {
    "analyses_result": {},
    "analyses_request": {},
    "corrections": {},
    "feedback": {},
    "analysis_events": [],
    "stack_feedback": {},
    "insights_feedback": [],
    "quality_criteria": {},
    "domains": {},
    "dep_categories": [],
    "dep_category_feedback": [],
    "taxonomy_domains": {},
    "taxonomy_categories": {},
}


def _now() -> datetime:
    return datetime.utcnow()


def _memory(name: str):
    return _memory_store.setdefault(name, [] if name in _LIST_STORES else {})


def _invalidate_taxonomy_cache() -> None:
    global _domain_cache, _category_cache
    _domain_cache = None
    _category_cache = None


async def seed_builtin_domains() -> None:
    if _db is not None:
        for domain in BUILTIN_DOMAINS:
            await _db.taxonomy_domains.update_one(
                {"_id": domain["_id"]},
                {"$setOnInsert": {
                    **{key: value for key, value in domain.items() if key != "_id"},
                    "created_at": _now(),
                }},
                upsert=True,
            )
    else:
        store = _memory("taxonomy_domains")
        for domain in BUILTIN_DOMAINS:
            store.setdefault(domain["_id"], {**domain, "created_at": _now()})
    _invalidate_taxonomy_cache()


async def seed_builtin_taxonomy_categories() -> None:
    records = [
        {
            "_id": category,
            "label": category.replace("_", " ").title(),
            "builtin": True,
            "active": True,
            "order": index,
        }
        for index, category in enumerate(BUILTIN_CATEGORIES, start=1)
    ]
    if _db is not None:
        for record in records:
            await _db.taxonomy_categories.update_one(
                {"_id": record["_id"]},
                {"$setOnInsert": {
                    **{key: value for key, value in record.items() if key != "_id"},
                    "created_at": _now(),
                }},
                upsert=True,
            )
    else:
        store = _memory("taxonomy_categories")
        for record in records:
            store.setdefault(record["_id"], {**record, "created_at": _now()})
    _invalidate_taxonomy_cache()


async def _taxonomy_rows(kind: str, include_inactive: bool = False) -> list[dict]:
    global _domain_cache, _category_cache
    cache = _domain_cache if kind == "domains" else _category_cache
    now = time.monotonic()
    if cache is not None and now - cache[0] < _TAXONOMY_CACHE_TTL:
        rows = cache[1]
    else:
        if _db is not None:
            collection = getattr(_db, f"taxonomy_{kind}")
            rows = [row async for row in collection.find({})]
        else:
            rows = list(_memory(f"taxonomy_{kind}").values())
        if not rows:
            if kind == "domains":
                rows = [dict(domain) for domain in BUILTIN_DOMAINS]
            else:
                rows = [
                    {
                        "_id": category,
                        "label": category.replace("_", " ").title(),
                        "builtin": True,
                        "active": True,
                        "order": index,
                    }
                    for index, category in enumerate(BUILTIN_CATEGORIES, start=1)
                ]
        cache = (now, rows)
        if kind == "domains":
            _domain_cache = cache
        else:
            _category_cache = cache
    active_rows = [row for row in rows if include_inactive or row.get("active", True)]
    return sorted(active_rows, key=lambda row: (row.get("order", 50), row["_id"]))


async def get_domains(include_inactive: bool = False) -> list[dict]:
    return await _taxonomy_rows("domains", include_inactive)


async def get_valid_domains() -> set[str]:
    return {row["_id"] for row in await get_domains()}


async def is_valid_domain(name: str) -> bool:
    return name in await get_valid_domains()


async def get_categories(include_inactive: bool = False) -> list[dict]:
    return await _taxonomy_rows("categories", include_inactive)


async def get_valid_categories() -> set[str]:
    return {row["_id"] for row in await get_categories()}


def _public_doc(doc: dict | None) -> dict | None:
    if not doc:
        return None
    out = deepcopy(doc)
    out.setdefault("analysis_id", out.get("_id"))
    out.setdefault("repo_key", out.get("_id"))
    return out


def _tech_names(stack: dict) -> list[str]:
    names: list[str] = []
    for techs in stack.values():
        if not isinstance(techs, list):
            continue
        for tech in techs:
            if isinstance(tech, dict) and tech.get("name"):
                names.append(tech["name"])
    return names


def _event_summary(stack: dict) -> dict:
    return {
        "domain": stack.get("domain"),
        "domain_confidence": stack.get("domain_confidence"),
        "stack_pattern": stack.get("stack_pattern"),
        "tech_names": _tech_names(stack),
        "quality_flags": stack.get("flags", []),
        "files_analyzed": stack.get("files_analyzed"),
        "ai_calls_made": stack.get("ai_calls_made"),
    }


def _feedback_training_row(row: dict) -> dict:
    rated_output = row.get("rated_output") or {}
    feedback = row.get("feedback") or {}
    domain_correct = feedback.get("domain_correct", True)
    correct_domain = (
        feedback.get("correct_domain")
        if domain_correct is False
        else rated_output.get("domain")
    )
    return {
        "feedback_id": row.get("_id") or row.get("feedback_id"),
        "repo_key": row.get("repo_key"),
        "commit_sha": row.get("commit_sha"),
        "pipeline_version": row.get("pipeline_version"),
        "repo_name": row.get("repo_key"),
        "detected_domain": rated_output.get("domain"),
        "correct_domain": correct_domain,
        "domain_correct": domain_correct,
        "stack_embedding": row.get("rated_embedding"),
        "primary_language": rated_output.get("primary_language"),
        "complexity_score": rated_output.get("complexity_score"),
        "ai_calls_made": rated_output.get("ai_calls_made"),
        "rated_output": rated_output,
        "rated_embedding": row.get("rated_embedding"),
        "feedback": feedback,
    }


async def init_db() -> None:
    global _client, _db
    uri = getenv("MONGODB_URI")
    if not uri:
        logger.warning("MONGODB_URI not set - using in-memory fallback")
        print("MONGODB_URI not set - using in-memory fallback")
        await seed_builtin_domains()
        await seed_builtin_taxonomy_categories()
        return

    _client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
    _db = _client[getenv("MONGODB_DB", "stacksniffer")]

    await _db.analyses_result.create_index("provider")
    await _db.analyses_result.create_index("owner")
    await _db.analyses_result.create_index("name")
    await _db.analyses_result.create_index("stack.domain")
    await _db.analyses_result.create_index("stack.stack_pattern")
    await _db.analyses_result.create_index("pipeline_version")
    await _db.analyses_result.create_index("stack_embedding", sparse=True)

    await _db.analyses_request.create_index("repo_key")
    await _db.analyses_request.create_index("created_at")
    await _db.corrections.create_index("repo_key")
    await _db.feedback.create_index("repo_key")
    await _db.feedback.create_index("created_at")
    await _db.analysis_events.create_index("repo_key")
    await _db.analysis_events.create_index("created_at")

    await _db.stack_feedback.create_index("analysis_id")
    await _db.stack_feedback.create_index("created_at")
    await _db.insights_feedback.create_index("analysis_id")
    await _db.insights_feedback.create_index("created_at")
    await _db.domains.create_index("domain_id", unique=True)
    await _db.dep_categories.create_index("category", unique=True)
    await _db.dep_category_feedback.create_index("category", unique=True)
    await _db.taxonomy_domains.create_index("active")
    await _db.taxonomy_categories.create_index("active")
    await seed_builtin_domains()
    await seed_builtin_taxonomy_categories()
    await seed_builtin_categories()   # idempotent, safe every boot
    logger.info("MongoDB connected: %s", getenv("MONGODB_DB", "stacksniffer"))
    print(f"MongoDB connected: {getenv('MONGODB_DB', 'stacksniffer')}")


async def close_db() -> None:
    if _client:
        _client.close()


def is_available() -> bool:
    return _db is not None


async def get_repo(repo_key: str, with_embedding: bool = False) -> dict | None:
    """
    Read one repo doc.

    with_embedding=False by default: stack_embedding is 3072 floats (~60KB of
    JSON) that no UI consumer reads. Only find_similar and the re-embed path
    need it. NOTE: the memory branch cannot project, so it always returns the
    embedding — an in-memory run and a Mongo run differ in shape here.
    """
    if _db is not None:
        projection = None if with_embedding else {"stack_embedding": 0}
        return _public_doc(
            await _db.analyses_result.find_one({"_id": repo_key}, projection)
        )
    return _public_doc(_memory("analyses_result").get(repo_key))


def is_fresh(doc: dict | None, head_sha: str, pipeline_version: str) -> bool:
    return bool(
        doc
        and doc.get("commit_sha") == head_sha
        and doc.get("pipeline_version") == pipeline_version
    )


async def upsert_repo_analysis(
    repo_key: str,
    stack: dict,
    embedding: list[float] | None,
    commit_sha: str,
    pipeline_version: str,
    repo_metadata: dict | None = None
) -> None:
    provider, owner, name = parse_repo_key(repo_key)
    now = _now()
    set_fields = {
        "commit_sha": commit_sha,
        "pipeline_version": pipeline_version,
        "stack": deepcopy(stack),
        "stack_embedding": embedding,
        "analyzed_at": now,
        "refresh": None,
    }
    set_on_insert = {
        "provider": provider,
        "owner": owner,
        "name": name,
        "created_at": now,
    }
    if repo_metadata is not None:
        set_fields["repo"] = deepcopy(repo_metadata)
    if _db is not None:
        await _db.analyses_result.update_one(
            {"_id": repo_key},
            {"$set": set_fields, "$setOnInsert": set_on_insert},
            upsert=True,
        )
        return

    store = _memory("analyses_result")
    doc = store.get(repo_key, {"_id": repo_key, **set_on_insert})
    doc.update(set_fields)
    store[repo_key] = doc


async def claim_refresh(
    repo_key,
    head_sha,
    pipeline_version,
    lease_minutes: int = 10,
) -> bool:
    provider, owner, name = parse_repo_key(repo_key)
    now = _now()
    refresh = {
        "status": "RUNNING",
        "target_sha": head_sha,
        "target_pipeline_version": pipeline_version,
        "started_at": now,
        "lease_expires_at": now + timedelta(minutes=lease_minutes),
    }
    refresh_filter = {
        "_id": repo_key,
        "$or": [
            {"refresh": None},
            {"refresh": {"$exists": False}},
            {"refresh.lease_expires_at": {"$lt": now}},
            # FAILED rows written before this fix carry no lease, so no other
            # branch can ever match them and the repo is wedged permanently.
            # One Gemini timeout would otherwise retire a repo for good.
            {"refresh.status": "FAILED"},
        ],
    }
    if _db is not None:
        result = await _db.analyses_result.update_one(
            refresh_filter,
            {"$set": {"refresh": refresh}},
            upsert=False,
        )
        if result.modified_count > 0:
            return True

        if await _db.analyses_result.find_one({"_id": repo_key}, {"_id": 1}):
            return False

        try:
            await _db.analyses_result.insert_one({
                "_id": repo_key,
                "provider": provider,
                "owner": owner,
                "name": name,
                "created_at": now,
                "refresh": refresh,
            })
            return True
        except DuplicateKeyError:
            return False

    store = _memory("analyses_result")
    doc = store.get(repo_key)
    current = doc.get("refresh") if doc else None
    expired = bool(
        current
        and current.get("lease_expires_at")
        and current["lease_expires_at"] < now
    )
    failed = bool(current and current.get("status") == "FAILED")
    if current is not None and not expired and not failed:
        return False
    if doc is None:
        doc = {
            "_id": repo_key,
            "provider": provider,
            "owner": owner,
            "name": name,
            "created_at": now,
        }
        store[repo_key] = doc
    doc["refresh"] = refresh
    return True


async def complete_refresh(
    repo_key,
    stack,
    embedding,
    commit_sha,
    pipeline_version,
    repo_metadata: dict | None = None,
) -> None:
    await upsert_repo_analysis(
        repo_key, stack, embedding, commit_sha, pipeline_version, repo_metadata
    )


async def fail_refresh(repo_key: str, error: str) -> None:
    now = _now()
    refresh = {
        "status": "FAILED",
        "error": str(error)[:500],
        "failed_at": now,
        # Already expired: a failed refresh must be immediately reclaimable.
        "lease_expires_at": now,
    }
    if _db is not None:
        await _db.analyses_result.update_one(
            {"_id": repo_key},
            {"$set": {"refresh": refresh}},
            upsert=True,
        )
        return
    _memory("analyses_result").setdefault(repo_key, {"_id": repo_key})["refresh"] = refresh


async def log_request(
    request_id,
    repo_key,
    repo_url_raw,
    head_sha_at_request,
    served: str,
) -> None:
    if served not in {"CACHED", "STALE", "ANALYZED", "HARD_REFRESH"}:
        raise ValueError("served must be one of CACHED, STALE, ANALYZED, HARD_REFRESH")
    doc = {
        "_id": request_id,
        "request_id": request_id,
        "repo_key": repo_key,
        "repo_url_raw": repo_url_raw,
        "head_sha_at_request": head_sha_at_request,
        "served": served,
        "created_at": _now(),
    }
    if _db is not None:
        await _db.analyses_request.insert_one(doc)
        return
    _memory("analyses_request")[request_id] = doc


async def request_to_repo_key(request_id: str) -> str | None:
    if _db is not None:
        doc = await _db.analyses_request.find_one({"_id": request_id}, {"repo_key": 1})
        return doc.get("repo_key") if doc else None
    doc = _memory("analyses_request").get(request_id)
    return doc.get("repo_key") if doc else None


async def upsert_correction(repo_key: str, field: str, value) -> None:
    if field not in ALLOWED_CORRECTION_FIELDS:
        raise ValueError(f"Unsupported correction field: {field}")
    doc = {
        "_id": f"{repo_key}:{field}",
        "repo_key": repo_key,
        "field": field,
        "value": value,
        "updated_at": _now(),
    }
    if _db is not None:
        await _db.corrections.update_one(
            {"_id": doc["_id"]},
            {"$set": doc, "$setOnInsert": {"created_at": doc["updated_at"]}},
            upsert=True,
        )
        return
    store = _memory("corrections")
    doc["created_at"] = store.get(doc["_id"], {}).get("created_at", doc["updated_at"])
    store[doc["_id"]] = doc


async def apply_corrections(repo_key: str, stack: dict) -> tuple[dict, bool]:
    out = deepcopy(stack)
    if _db is not None:
        corrections = await _db.corrections.find(
            {"repo_key": repo_key}, {"_id": 0}
        ).to_list(100)
    else:
        corrections = [
            c for c in _memory("corrections").values()
            if c.get("repo_key") == repo_key
        ]

    touched = False
    for correction in corrections:
        field = correction.get("field")
        if field not in ALLOWED_CORRECTION_FIELDS:
            continue
        value = correction.get("value")
        if out.get(field) != value:
            out[field] = value
            touched = True
    return out, touched


async def record_event(repo_key, commit_sha, pipeline_version, summary: dict) -> None:
    doc = {
        "repo_key": repo_key,
        "commit_sha": commit_sha,
        "pipeline_version": pipeline_version,
        "summary": deepcopy(summary),
        "created_at": _now(),
    }
    if _db is not None:
        await _db.analysis_events.insert_one(doc)
        return
    _memory("analysis_events").append(doc)


async def get_analysis_events(repo_key: str | None = None) -> list[dict]:
    if _db is not None:
        query = {"repo_key": repo_key} if repo_key else {}
        cursor = _db.analysis_events.find(query, {"_id": 0}).sort("created_at", ASCENDING)
        return await cursor.to_list(10000)
    events = list(_memory("analysis_events"))
    if repo_key:
        events = [e for e in events if e.get("repo_key") == repo_key]
    return sorted(events, key=lambda e: e.get("analyzed_at") or e.get("created_at"))


async def count_corrections() -> int:
    if _db is not None:
        return await _db.corrections.count_documents({})
    return len(_memory("corrections"))


async def store_feedback(
    repo_key,
    commit_sha,
    pipeline_version,
    rated_output: dict,
    rated_embedding: list[float],
    feedback: dict,
) -> str:
    feedback_id = str(uuid4())
    doc = {
        "_id": feedback_id,
        "feedback_id": feedback_id,
        "repo_key": repo_key,
        "commit_sha": commit_sha,
        "pipeline_version": pipeline_version,
        "rated_output": deepcopy(rated_output),
        "rated_embedding": list(rated_embedding or []),
        "feedback": deepcopy(feedback),
        "created_at": _now(),
    }
    # Do NOT spread `feedback` over the top level. It is caller-supplied, and a
    # body containing repo_key/commit_sha/rated_output would silently overwrite
    # the real ones — corrupting exactly the provenance this row exists to
    # freeze. Promote only the known scoring keys.
    for key in ("domain_correct", "correct_domain", "rating", "source", "notes"):
        if key in feedback:
            doc[key] = deepcopy(feedback[key])
    if _db is not None:
        await _db.feedback.insert_one(doc)
        return feedback_id
    _memory("feedback")[feedback_id] = doc
    return feedback_id


async def get_labeled_training_data() -> list[dict]:
    if _db is not None:
        rows = await _db.feedback.find({}, {"_id": 0}).to_list(10000)
    else:
        rows = list(_memory("feedback").values())
    return [_feedback_training_row(row) for row in rows]


async def get_analysis(analysis_id: str) -> dict | None:
    repo_key = analysis_id
    if ":" not in analysis_id:
        repo_key = await request_to_repo_key(analysis_id) or analysis_id
    return await get_repo(repo_key)


async def get_all_analyses(with_embeddings_only: bool = False) -> list[dict]:
    if _db is not None:
        query = {"stack_embedding": {"$exists": True, "$ne": None}} if with_embeddings_only else {}
        cursor = _db.analyses_result.find(query)
        return [_public_doc(doc) for doc in await cursor.to_list(10000)]
    docs = [_public_doc(v) for v in _memory("analyses_result").values()]
    if with_embeddings_only:
        docs = [d for d in docs if d.get("stack_embedding")]
    return docs


async def count_embedded_analyses() -> int:
    if _db is not None:
        return await _db.analyses_result.count_documents(
            {"stack_embedding": {"$exists": True, "$ne": None}}
        )
    return sum(1 for v in _memory("analyses_result").values() if v.get("stack_embedding"))


async def find_similar(
    embedding: list[float],
    limit: int = 5,
    exclude_repo_key=None,
) -> list[dict]:
    if _db is None:
        results = []
        for doc in _memory("analyses_result").values():
            if exclude_repo_key and doc.get("_id") == exclude_repo_key:
                continue
            if doc.get("pipeline_version") != PIPELINE_VERSION:
                continue
            if not doc.get("stack_embedding"):
                continue
            results.append(_public_doc({**doc, "score": 0.0}))
        return results[:limit]

    try:
        match_filter = {"stack_embedding": {"$exists": True, "$ne": None}}
        if exclude_repo_key:
            match_filter["_id"] = {"$ne": exclude_repo_key}
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "stack_vector_index",
                    "path": "stack_embedding",
                    "queryVector": embedding,
                    "numCandidates": 100,
                    "limit": 25,
                    "filter": {"pipeline_version": PIPELINE_VERSION},
                }
            },
            {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
            {"$match": match_filter},
            {"$sort": {"score": DESCENDING}},
            {"$group": {"_id": "$_id", "doc": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$doc"}},
            {"$sort": {"score": DESCENDING}},
            {"$limit": limit},
            {
                "$project": {
                    "_id": 0,
                    "analysis_id": "$_id",
                    "repo_key": "$_id",
                    "provider": 1,
                    "owner": 1,
                    "name": 1,
                    "repo.full_name": 1,
                    "repo.description": 1,
                    "repo.stars": 1,
                    "stack.domain": 1,
                    "stack.domain_confidence": 1,
                    "stack.domain_reasoning": 1,
                    "stack.stack_pattern": 1,
                    "stack.why_this_stack": 1,
                    "stack.primary_language": 1,
                    "stack.architecture_style": 1,
                    "score": 1,
                }
            },
        ]
        cursor = _db.analyses_result.aggregate(pipeline)
        return await cursor.to_list(limit)
    except Exception as e:
        logger.error("Vector search failed: %s", str(e)[:300])
        return []


async def find_similar_by_domain(domain: str, limit: int = 3) -> list[dict]:
    if _db is None:
        return [
            _public_doc(v) for v in _memory("analyses_result").values()
            if v.get("stack", {}).get("domain") == domain and v.get("stack_embedding")
        ][:limit]
    cursor = _db.analyses_result.find(
        {"stack.domain": domain, "stack_embedding": {"$exists": True, "$ne": None}},
        {
            "_id": 0,
            "analysis_id": "$_id",
            "repo_key": "$_id",
            "stack.domain": 1,
            "stack.domain_reasoning": 1,
            "stack.stack_pattern": 1,
            "stack.why_this_stack": 1,
        },
    ).sort("analyzed_at", DESCENDING).limit(limit)
    return await cursor.to_list(limit)


async def get_all_feedback(min_count: int = 0) -> list[dict]:
    if _db is not None:
        return await _db.feedback.find({}, {"_id": 0}).to_list(10000)
    return list(_memory("feedback").values())


async def get_feedback_for_analysis(analysis_id: str) -> dict | None:
    repo_key = await request_to_repo_key(analysis_id) or analysis_id
    if _db is not None:
        return await _db.feedback.find_one({"repo_key": repo_key}, {"_id": 0})
    for row in _memory("feedback").values():
        if row.get("repo_key") == repo_key:
            return row
    return None


async def store_stack_feedback(analysis_id: str, evaluation: dict) -> None:
    doc = {
        "analysis_id": analysis_id,
        "created_at": _now(),
        "tech_evaluations": [evaluation],
    }
    if _db is not None:
        await _db.stack_feedback.insert_one(doc)
    else:
        _memory("stack_feedback").setdefault(analysis_id, []).append(doc)


async def get_all_stack_feedback() -> list[dict]:
    if _db is not None:
        return await _db.stack_feedback.find({}, {"_id": 0}).to_list(10000)
    return [item for entries in _memory("stack_feedback").values() for item in entries]


async def store_insights_feedback(analysis_id: str, doc: dict) -> None:
    record = {**doc, "analysis_id": analysis_id, "created_at": _now()}
    if _db is not None:
        await _db.insights_feedback.insert_one(record)
    else:
        _memory("insights_feedback").append(record)


async def get_all_insights_feedback() -> list[dict]:
    if _db is not None:
        return await _db.insights_feedback.find({}, {"_id": 0}).to_list(10000)
    return _memory("insights_feedback")


async def get_quality_criteria() -> list[dict]:
    if _db is not None:
        cursor = _db.quality_criteria.find(
            {"field": {"$ne": "_overall"}}, {"_id": 0}
        ).sort("field", ASCENDING)
        return await cursor.to_list(20)
    return [
        v for v in _memory("quality_criteria").values()
        if v.get("field") != "_overall"
    ]


async def update_quality_criterion(field: str, criterion: dict) -> None:
    doc = {**criterion, "field": field, "updated_at": _now()}
    if _db is not None:
        await _db.quality_criteria.update_one(
            {"field": field},
            {"$set": doc},
            upsert=True,
        )
    else:
        _memory("quality_criteria")[field] = doc


async def get_all_domains() -> list[dict]:
    if _db is not None:
        cursor = _db.domains.find(
            {"status": "active"}, {"_id": 0}
        ).sort("domain_id", ASCENDING)
        return await cursor.to_list(100)
    return [
        d for d in _memory("domains").values()
        if d.get("status", "active") == "active"
    ]


async def upsert_domain(domain_id: str, doc: dict) -> None:
    record = {**doc, "domain_id": domain_id}
    if _db is not None:
        await _db.domains.update_one(
            {"domain_id": domain_id},
            {"$set": record},
            upsert=True,
        )
    else:
        _memory("domains")[domain_id] = record


async def store_domain(domain_id: str, doc: dict) -> None:
    await upsert_domain(domain_id, doc)


async def delete_domain(domain_id: str) -> None:
    if _db is not None:
        await _db.domains.delete_one({"domain_id": domain_id})
    else:
        _memory("domains").pop(domain_id, None)


async def store_emergent_categories(entries: list[dict]) -> None:
    if not entries:
        return
    if _db is not None:
        for entry in entries:
            await _db.dep_categories.update_one(
                {"category": entry["category"]},
                {
                    "$set": {"category": entry["category"]},
                    "$addToSet": {
                        "example_techs": entry.get("example_tech", ""),
                        "example_repos": entry.get("example_repo", ""),
                    },
                    "$inc": {"seen_count": 1},
                    "$setOnInsert": {"standard": False, "status": "pending"},
                },
                upsert=True,
            )
        return

    store = _memory("dep_categories")
    for entry in entries:
        existing = next((c for c in store if c["category"] == entry["category"]), None)
        if existing:
            existing["seen_count"] = existing.get("seen_count", 0) + 1
        else:
            store.append({
                **entry,
                "seen_count": 1,
                "standard": False,
                "status": "pending",
            })


async def get_dep_categories() -> list[dict]:
    from backend.services.category_registry import BUILTIN_CATEGORIES
    standard = [
        {"category": c, "standard": True, "status": "active", "seen_count": 0}
        for c in BUILTIN_CATEGORIES
    ]
    if _db is not None:
        emergent = await _db.dep_categories.find(
            {"standard": {"$ne": True}}, {"_id": 0}
        ).sort("seen_count", DESCENDING).to_list(200)
        return standard + emergent
    return standard + _memory("dep_categories")


async def get_category_feedback_decisions() -> dict:
    if _db is not None:
        feedback = await _db.dep_category_feedback.find({}, {"_id": 0}).to_list(500)
    else:
        feedback = _memory("dep_category_feedback")

    discarded: list[str] = []
    merged: dict[str, str] = {}
    promoted: list[str] = []
    for row in feedback:
        category = row.get("category", "")
        action = row.get("action", "")
        if action == "discard":
            discarded.append(category)
        elif action == "merge" and row.get("merge_into"):
            merged[category] = row["merge_into"]
        elif action == "promote":
            promoted.append(category)
    return {"discarded": discarded, "merged": merged, "promoted": promoted}


async def store_category_feedback(
    category: str,
    action: str,
    merge_into: str = None,
    reason: str = None,
    source: str = "ui",
) -> dict:
    doc = {
        "category": category,
        "action": action,
        "merge_into": merge_into,
        "reason": reason,
        "source": source,
        "created_at": _now(),
    }
    if _db is not None:
        await _db.dep_category_feedback.update_one(
            {"category": category},
            {"$set": doc},
            upsert=True,
        )
        await _db.dep_categories.update_one(
            {"category": category},
            {"$set": {"status": action}},
        )
    else:
        store = _memory("dep_category_feedback")
        existing = next((f for f in store if f["category"] == category), None)
        if existing:
            existing.update(doc)
        else:
            store.append(doc)
    return doc


async def delete_category_feedback(category: str) -> None:
    if _db is not None:
        await _db.dep_category_feedback.delete_one({"category": category})
        await _db.dep_categories.update_one(
            {"category": category},
            {"$set": {"status": "pending"}},
        )
    else:
        _memory_store["dep_category_feedback"] = [
            f for f in _memory("dep_category_feedback")
            if f["category"] != category
        ]


async def reclassify_category_techs(from_category: str, to_category: str | None) -> int:
    if _db is None:
        count = 0
        for doc in _memory("analyses_result").values():
            stack = doc.get("stack", {})
            techs = stack.get(from_category, [])
            if not techs:
                continue
            if to_category is None:
                stack.pop(from_category, None)
            else:
                stack.setdefault(to_category, []).extend(techs)
                stack.pop(from_category, None)
            count += 1
        return count

    cursor = _db.analyses_result.find(
        {f"stack.{from_category}": {"$exists": True, "$ne": []}},
        {f"stack.{from_category}": 1},
    )
    count = 0
    async for doc in cursor:
        techs = doc.get("stack", {}).get(from_category, [])
        if not techs:
            continue
        if to_category is None:
            update = {"$unset": {f"stack.{from_category}": ""}}
        else:
            update = {
                "$push": {f"stack.{to_category}": {"$each": techs}},
                "$unset": {f"stack.{from_category}": ""},
            }
        await _db.analyses_result.update_one({"_id": doc["_id"]}, update)
        count += 1
    return count


async def promote_category(category: str) -> None:
    if _db is not None:
        await _db.dep_categories.update_one(
            {"category": category},
            {"$set": {"standard": True, "status": "active"}},
            upsert=True,
        )
    else:
        store = _memory("dep_categories")
        existing = next((c for c in store if c["category"] == category), None)
        if existing:
            existing["standard"] = True
            existing["status"] = "active"
        else:
            store.append({
                "category": category,
                "standard": True,
                "status": "active",
                "seen_count": 0,
            })
    from backend.services.category_registry import invalidate_cache
    invalidate_cache()


async def update_category_metadata(
    category: str,
    display_name: str = None,
    description: str = None,
    color: str = None,
) -> None:
    update = {}
    if display_name is not None:
        update["display_name"] = display_name
    if description is not None:
        update["description"] = description
    if color is not None:
        update["color"] = color
    if not update:
        return
    if _db is not None:
        await _db.dep_categories.update_one(
            {"category": category},
            {"$set": update},
            upsert=True,
        )
    else:
        store = _memory("dep_categories")
        existing = next((c for c in store if c["category"] == category), None)
        if existing:
            existing.update(update)
        else:
            store.append({"category": category, **update})


async def find_by_domain(domain: str, limit: int = 20) -> list[dict]:
    if _db is None:
        return [
            _public_doc(v) for v in _memory("analyses_result").values()
            if v.get("stack", {}).get("domain") == domain
        ][:limit]
    cursor = _db.analyses_result.find(
        {"stack.domain": domain},
        {
            "_id": 0,
            "analysis_id": "$_id",
            "repo_key": "$_id",
            "stack.why_this_stack": 1,
            "stack.stack_pattern": 1,
            "stack.primary_language": 1,
            "analyzed_at": 1,
        },
    ).sort("analyzed_at", DESCENDING).limit(limit)
    return await cursor.to_list(limit)


async def find_by_stack_pattern(pattern: str, limit: int = 10) -> list[dict]:
    if _db is None:
        needle = pattern.lower()
        return [
            _public_doc(v) for v in _memory("analyses_result").values()
            if needle in (v.get("stack", {}).get("stack_pattern") or "").lower()
        ][:limit]
    cursor = _db.analyses_result.find(
        {"stack.stack_pattern": {"$regex": pattern, "$options": "i"}},
        {
            "_id": 0,
            "analysis_id": "$_id",
            "repo_key": "$_id",
            "stack.stack_pattern": 1,
        },
    ).sort("analyzed_at", DESCENDING).limit(limit)
    return await cursor.to_list(limit)


async def get_stats() -> dict:
    if _db is None:
        analyses = list(_memory("analyses_result").values())
        return {
            "total_analyses": len(analyses),
            "storage": "memory",
            "with_embeddings": sum(1 for a in analyses if a.get("stack_embedding")),
            "with_feedback": len(_memory("feedback")),
        }

    total = await _db.analyses_result.count_documents({})
    with_embeddings = await _db.analyses_result.count_documents(
        {"stack_embedding": {"$exists": True, "$ne": None}}
    )
    with_feedback = await _db.feedback.count_documents({})
    with_stack_fb = await _db.stack_feedback.count_documents({})
    with_insights_fb = await _db.insights_feedback.count_documents({})
    emergent_cats = await _db.dep_categories.count_documents(
        {"standard": {"$ne": True}}
    )
    pending_review = await _db.dep_categories.count_documents(
        {"standard": {"$ne": True}, "status": "pending"}
    )
    by_domain = await _db.analyses_result.aggregate([
        {"$group": {"_id": "$stack.domain", "count": {"$sum": 1}}},
        {"$sort": {"count": DESCENDING}},
    ]).to_list(20)
    return {
        "storage": "mongodb",
        "total_analyses": total,
        "with_embeddings": with_embeddings,
        "embedding_coverage": f"{with_embeddings / max(total, 1) * 100:.0f}%",
        "with_feedback": with_feedback,
        "with_stack_feedback": with_stack_fb,
        "with_insights_feedback": with_insights_fb,
        "emergent_categories": emergent_cats,
        "pending_category_review": pending_review,
        "by_domain": {d["_id"]: d["count"] for d in by_domain if d["_id"]},
    }

from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc)


async def seed_builtin_categories() -> None:
    """
    Idempotent. Ensures the 8 builtins exist as standard=True rows so
    get_promoted_categories() and the registry share one source. Run once at
    startup (or in seed_corpus).
    """
    from backend.services.category_registry import BUILTIN_CATEGORIES
    if _db is not None:
        for cat in BUILTIN_CATEGORIES:
            await _db.dep_categories.update_one(
                {"category": cat},
                {"$setOnInsert": {
                    "category": cat,
                    "standard": True,
                    "status": "active",
                    "seen_count": 0,
                }},
                upsert=True,
            )
        return

    store = _memory("dep_categories")
    known = {row.get("category") for row in store}
    store.extend(
        {"category": cat, "standard": True, "status": "active", "seen_count": 0}
        for cat in BUILTIN_CATEGORIES
        if cat not in known
    )


async def record_emergent_category(name: str, example_tech: str, example_repo: str) -> None:
    """
    Called by _store_emergent_categories when Gemini emits a non-builtin
    category. Accumulates evidence; does NOT promote. standard stays False until
    a human acts. Bumps a sighting counter so the review UI can rank by frequency
    ('bundler seen on 6 repos' is a stronger promote signal than one sighting).
    """
    await store_emergent_categories([{
        "category": name,
        "example_tech": example_tech,
        "example_repo": example_repo,
    }])


async def get_promoted_categories() -> list[str]:
    """Every category the registry should treat as valid: standard=True rows."""
    if _db is not None:
        cursor = _db.dep_categories.find({"standard": True}, {"category": 1})
        return [doc["category"] async for doc in cursor if doc.get("category")]
    return [
        row["category"] for row in _memory("dep_categories")
        if row.get("standard") and row.get("category")
    ]


async def get_pending_categories() -> list[dict]:
    """Emergent categories awaiting a human decision, most-sighted first."""
    if _db is not None:
        rows = await _db.dep_categories.find(
            {"standard": {"$ne": True}, "status": "pending"}, {"_id": 0}
        ).sort("seen_count", -1).to_list(200)
    else:
        rows = sorted(
            (
                row for row in _memory("dep_categories")
                if not row.get("standard") and row.get("status") == "pending"
            ),
            key=lambda row: row.get("seen_count", 0),
            reverse=True,
        )
    return [
        {
            **row,
            "_id": row.get("category"),
            "sightings": row.get("seen_count", 0),
            "last_example_tech": (
                (row.get("example_techs") or [None])[-1]
                if isinstance(row.get("example_techs"), list)
                else row.get("example_tech")
            ),
        }
        for row in rows
    ]


async def discard_category(name: str) -> dict:
    """
    Reject an emergent category. Techs Gemini put there will be remapped to
    'library' or 'dev_tool' on future runs (the prompt's DISCARDED instruction).
    Builtins cannot be discarded.
    """
    from backend.services.category_registry import BUILTIN_CATEGORIES, invalidate_cache
    if name in BUILTIN_CATEGORIES:
        return {"ok": False, "error": f"'{name}' is a builtin category and cannot be discarded"}

    await _db.dep_categories.update_one(
        {"_id": name},
        {"$set": {"standard": False, "status": "discarded", "discarded_at": _now()}},
        upsert=True,
    )
    invalidate_cache()
    return {"ok": True, "name": name}


async def merge_category(name: str, into: str) -> dict:
    """
    Fold an emergent category into an existing one (e.g. 'bundler' -> 'infra').
    The prompt's MERGED instruction then tells Gemini to classify {name} as {into}.
    """
    from backend.services.category_registry import invalidate_cache
    await _db.dep_categories.update_one(
        {"_id": name},
        {"$set": {"standard": False, "status": "merged", "merged_into": into,
                  "merged_at": _now()}},
        upsert=True,
    )
    invalidate_cache()
    return {"ok": True, "name": name, "merged_into": into}
