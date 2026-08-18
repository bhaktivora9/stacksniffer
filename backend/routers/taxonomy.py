"""
backend/routers/taxonomy.py

Emergent software_type taxonomy discovery and management.
Java equivalent: PatternClusterer.java + DynamicPatternConfigService.java
in stacksniffer-learning, feeding software_type-definitions.yml in stacksniffer-config.

The taxonomy flow:
  1. POST /discover   → DBSCAN clusters embedded analyses
  2. Human reviews cluster summaries (suggested names)
  3. POST /approve    → approved clusters stored in MongoDB software_types collection
  4. GET /software_types     → ai_pipeline_rag._get_software_type_options() reads this
  5. Gemini prompt uses extended software_type list automatically
  6. New software_types (e.g. "monitoring-infra") appear without code deploy

Java equivalent flow:
  PatternClusterer.clusterPatterns() → DynamicPatternConfigService.updateSoftwareTypeConfig()
  → PatternConfigUpdatedEvent → PatternsReloadedEvent → software_type-definitions.yml updated
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from backend.services import storage_service as storage_service
from backend.services import taxonomy_discovery
from backend.services.ai_pipeline import get_stack_pattern_taxonomy

router = APIRouter(prefix="/api/taxonomy", tags=["taxonomy"])


class ApproveRequest(BaseModel):
    approved_names: dict[str, str]  # {"cluster_0": "monitoring-infra"}
    quality_threshold: float = 0.0  # reject if silhouette below this


class AddSoftwareTypeRequest(BaseModel):
    software_type_id: str                   # "monitoring-infra"
    label: str                       # "Monitoring & Observability"
    tech_signals: list[str] = []     # ["Prometheus", "Grafana", "InfluxDB"]
    primary_languages: list[str] = []
    parent_software_type: Optional[str] = None
    notes: Optional[str] = None


class TaxonomyActionRequest(BaseModel):
    action: str
    merge_into: Optional[str] = None


class EmergentSuggestionRequest(BaseModel):
    kind: str
    name: str
    example_repo: str = "manual-ui"
    example_tech: Optional[str] = None


# ── Clustering ────────────────────────────────────────────────────────────────

@router.post("/discover")
async def discover_taxonomy(
    method: str = "dbscan",
    eps: float = 0.25,
    min_samples: int = 2,
    n_clusters: int = 8
):
    """
    Run DBSCAN clustering on embedded analyses to discover natural software_type groups.

    Requires: 10+ embedded analyses in MongoDB.
    Recommended: 50+ for trustworthy taxonomy (silhouette > 0.3).

    Returns cluster summaries for human review — does NOT auto-apply.
    Human must POST /approve with approved_names.

    Java equivalent: PatternClusterer.clusterPatterns() in stacksniffer-learning.

    Parameters:
      method:      "dbscan" (recommended — discovers cluster count)
                   "kmeans" (forces fixed cluster count)
      eps:         DBSCAN distance threshold (0.15=tight, 0.25=balanced, 0.40=loose)
      min_samples: minimum repos per cluster (2 for small corpus, 5 for large)
    """
    try:
        return await taxonomy_discovery.discover_taxonomy(
            method=method,
            eps=eps,
            min_samples=min_samples,
            n_clusters=n_clusters
        )
    except ImportError as e:
        raise HTTPException(422, f"Missing dependency: {e}. Run: pip install scikit-learn numpy")
    except Exception as e:
        raise HTTPException(500, f"Clustering failed: {str(e)[:300]}")


@router.post("/approve")
async def approve_taxonomy(request: ApproveRequest):
    """
    Store human-approved cluster names as active software_type taxonomy.

    After POST /discover, review cluster summaries and submit approved names.
    Only explicitly approved clusters become software_types.

    Java equivalent: DynamicPatternConfigService.updateSoftwareTypeConfig()
    which writes to software_type-definitions.yml and publishes PatternConfigUpdatedEvent.
    """
    discovery = await taxonomy_discovery.discover_taxonomy()
    if discovery.get("status") == "insufficient_data":
        raise HTTPException(422, discovery["message"])

    quality = discovery.get("quality", {})
    silhouette = quality.get("silhouette_score", 0)

    if silhouette < request.quality_threshold:
        raise HTTPException(
            422,
            f"Cluster quality too low (silhouette={silhouette:.3f} < "
            f"threshold={request.quality_threshold}). "
            f"Collect more repos or adjust eps."
        )

    return await taxonomy_discovery.store_discovered_taxonomy(
        discovery["clusters"],
        request.approved_names,
        quality
    )


# ── SoftwareType CRUD ───────────────────────────────────────────────────────────────

@router.get("/software_types")
async def list_software_types():
    """
    Return all active software_types — used by:
      1. Frontend dropdown (instead of hardcoded list)
      2. ai_pipeline_rag._get_software_type_options() for Gemini prompt

    Combines: system defaults + DBSCAN-discovered + manually added.
    Java equivalent: software_type-definitions.yml loaded by SoftwareTypeDefinition.java
    via DynamicPatternConfigService.
    """
    software_types = await storage_service.get_software_types()
    return {"software_types": [
        {
            "id": software_type["_id"],
            "label": software_type.get("label", software_type["_id"]),
            "sentinel": software_type.get("sentinel", False),
        }
        for software_type in software_types
    ]}


@router.get("/specific_identities")
async def list_specific_identity_counts():
    """Return observed finer-grained identities ranked for promotion review."""
    identities = await storage_service.get_specific_identity_counts()
    return {"specific_identities": identities, "count": len(identities)}


@router.get("/technology_roles")
async def list_technology_roles():
    technology_roles = await storage_service.get_technology_roles()
    return {"technology_roles": [
        {"id": technology_role["_id"], "label": technology_role.get("label", technology_role["_id"])}
        for technology_role in technology_roles
    ]}


@router.get("/stack_patterns")
async def list_stack_patterns():
    return get_stack_pattern_taxonomy()


@router.get("/search")
async def search_taxonomy_examples(kind: str, q: str, limit: int = 10):
    if kind not in {"technology", "software_type", "technology_role"}:
        raise HTTPException(400, "kind must be technology, software_type, or technology_role")
    results = await storage_service.search_analysis_examples(kind, q, min(max(limit, 1), 25))
    return {"kind": kind, "query": q, "examples": results, "count": len(results)}


@router.get("/pending")
async def list_pending_taxonomy():
    return await storage_service.get_pending_taxonomy()


@router.post("/pending")
async def suggest_pending_taxonomy(request: EmergentSuggestionRequest):
    if request.kind not in {"software_type", "technology_role"}:
        raise HTTPException(400, "kind must be software_type or technology_role")
    name = request.name.strip().lower().replace(" ", "_")
    if not name or not all(character.isalnum() or character == "_" for character in name):
        raise HTTPException(400, "name must contain only letters, numbers, spaces, or underscores")
    valid = (
        await storage_service.get_valid_software_types()
        if request.kind == "software_type" else await storage_service.get_valid_technology_roles()
    )
    if name in valid:
        raise HTTPException(409, f"{name} is already an active {request.kind}")
    if request.kind == "software_type":
        await storage_service.record_emergent_software_type(name, request.example_repo)
    else:
        await storage_service.record_emergent_technology_role(
            name, request.example_tech or "manual suggestion", request.example_repo
        )
    return {"ok": True, "kind": request.kind, "name": name, "status": "pending"}


@router.post("/{kind}/{name}/action")
async def apply_taxonomy_action(kind: str, name: str, request: TaxonomyActionRequest):
    if kind not in {"software_type", "technology_role"}:
        raise HTTPException(400, "kind must be software_type or technology_role")
    if request.action not in {"promote", "merge", "discard"}:
        raise HTTPException(400, "action must be promote, merge, or discard")
    valid_targets = (
        await storage_service.get_valid_software_types()
        if kind == "software_type" else await storage_service.get_valid_technology_roles()
    )
    if request.action == "merge" and request.merge_into not in valid_targets:
        raise HTTPException(400, "merge_into must be an active taxonomy value")
    try:
        return await storage_service.apply_taxonomy_action(
            kind, name, request.action, request.merge_into
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/software_types")
async def add_software_type(request: AddSoftwareTypeRequest):
    """
    Manually add a software_type not discovered by clustering.
    Use for edge cases: "blockchain", "embedded", "game-engine", "desktop-app".
    Java equivalent: manually editing software_type-definitions.yml.
    """
    doc = {
        "software_type_id":        request.software_type_id,
        "label":            request.label,
        "tech_signals":     request.tech_signals,
        "primary_languages": request.primary_languages,
        "parent_software_type":    request.parent_software_type,
        "notes":            request.notes,
        "source":           "manual",
        "usage_count":      0,
        "created_at":       datetime.utcnow().isoformat(),
        "status":           "active"
    }
    await storage_service.store_software_type(request.software_type_id, doc)
    return {"created": True, "software_type_id": request.software_type_id}


@router.delete("/software_types/{software_type_id}")
async def remove_software_type(software_type_id: str):
    """Soft delete — marks inactive, preserves history."""
    await storage_service.delete_software_type(software_type_id)
    return {"deleted": True, "software_type_id": software_type_id}



def _suggest_cluster_name(frameworks, languages, ai_ml, patterns):
    fw_lower   = [f.lower() for f in frameworks]
    ai_lower   = [a.lower() for a in ai_ml]
    lang_lower = [l.lower() for l in languages]

    # language runtime signals
    lang_runtime = ["lexer", "parser", "ast", "bytecode", "runtime", "compiler"]
    if any(s in " ".join(fw_lower + lang_lower) for s in lang_runtime):
        return "language"

    # database/storage signals
    db_signals = ["wal", "lsm", "shard", "replication", "compaction",
                  "storage-engine", "vector-store", "search-engine"]
    if any(s in " ".join(fw_lower) for s in db_signals):
        return "database"

    # ml_platform signals — AI IS the product
    llm_frameworks = {"langchain", "llamaindex", "autogen", "crewai",
                      "dspy", "litellm", "vllm", "triton"}
    if any(x in ai_lower for x in llm_frameworks):
        return "ml_platform"
    if len(ai_ml) >= 3:
        return "ml_platform"

    # infra_tool signals
    infra_signals = {"terraform", "ansible", "pulumi", "helm", "prometheus",
                     "grafana", "istio", "skaffold", "argo"}
    if any(f in infra_signals for f in fw_lower):
        return "infra_tool"
    if "go" in lang_lower and not any(x in fw_lower for x in
       ["gin", "echo", "fiber", "chi"]):
        return "infra_tool"

    # data_pipeline signals
    pipeline_signals = {"kafka", "spark", "flink", "airflow", "dagster",
                        "prefect", "dbt", "beam"}
    if any(f in pipeline_signals for f in fw_lower):
        return "data_pipeline"

    # web_app signals — frontend present
    frontend = {"react", "next.js", "vue", "angular", "svelte"}
    backend  = {"fastapi", "django", "spring boot", "express", "nestjs"}
    if any(f in frontend for f in fw_lower):
        return "web_app"

    # web_api signals — backend only
    if any(f in backend for f in fw_lower):
        return "web_api"

    # library — no server, no app
    return "library"

# ── Gemini prompt integration ──────────────────────────────────────────────────

@router.get("/prompt-fragment")
async def get_software_type_prompt_fragment():
    """
    Returns the software_type classification string injected into Gemini prompt.
    ai_pipeline_rag._get_software_type_options() calls this internally.

    Example output:
      "web_api | data_pipeline | ml_platform | ... | monitoring-infra | unknown"

    New software_types discovered by DBSCAN appear here automatically after approval.
    Java equivalent: SoftwareTypeDefinition.getAllSoftwareTypeIds() used by GeminiServiceImpl.
    """
    software_type_ids = [software_type["_id"] for software_type in await storage_service.get_software_types()]

    return {
        "fragment":     " | ".join(software_type_ids),
        "software_type_count": len(software_type_ids),
        "software_types":      software_type_ids
    }


# ── Visualization ─────────────────────────────────────────────────────────────

@router.get("/cluster-preview")
async def cluster_preview(eps: float = 0.25, min_samples: int = 2):
    """
    2D projection of embedding clusters for scatter plot visualization.
    Uses UMAP if installed, falls back to PCA.

    Frontend can render this as an interactive scatter plot showing
    how repos cluster by tech stack similarity.
    """
    embeddings, analysis_ids, metadata = \
        await taxonomy_discovery.load_embeddings_for_clustering()

    if len(embeddings) < 5:
        return {
            "error": f"Need at least 5 embedded analyses, have {len(embeddings)}",
            "count": len(embeddings)
        }

    try:
        import numpy as np
        from sklearn.preprocessing import normalize
        from sklearn.decomposition import PCA

        X = normalize(np.array(embeddings))
        labels = taxonomy_discovery.cluster_dbscan(embeddings, eps=eps, min_samples=min_samples)

        try:
            import umap
            reducer = umap.UMAP(n_components=2, random_state=42, metric="cosine")
            X_2d = reducer.fit_transform(X)
            reduction_method = "umap"
        except ImportError:
            pca = PCA(n_components=2)
            X_2d = pca.fit_transform(X)
            reduction_method = "pca"

        quality = taxonomy_discovery.compute_cluster_quality(embeddings, labels)

        return {
            "points": [
                {
                    "x":           float(X_2d[i][0]),
                    "y":           float(X_2d[i][1]),
                    "cluster":     labels[i],
                    "repo":        metadata[i]["repo"],
                    "software_type":      metadata[i]["current_software_type"],
                    "language":    metadata[i]["primary_language"],
                    "analysis_id": analysis_ids[i]
                }
                for i in range(len(embeddings))
            ],
            "reduction_method": reduction_method,
            "n_clusters":       len(set(l for l in labels if l != -1)),
            "silhouette":       quality.get("silhouette_score"),
            "total_repos":      len(embeddings)
        }

    except ImportError as e:
        raise HTTPException(422, f"Missing dependency: {e}. Run: pip install scikit-learn numpy")
