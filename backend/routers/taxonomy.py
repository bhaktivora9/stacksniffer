"""
backend/routers/taxonomy.py

Emergent domain taxonomy discovery and management.
Java equivalent: PatternClusterer.java + DynamicPatternConfigService.java
in stacksniffer-learning, feeding domain-definitions.yml in stacksniffer-config.

The taxonomy flow:
  1. POST /discover   → DBSCAN clusters embedded analyses
  2. Human reviews cluster summaries (suggested names)
  3. POST /approve    → approved clusters stored in MongoDB domains collection
  4. GET /domains     → ai_pipeline_rag._get_domain_options() reads this
  5. Gemini prompt uses extended domain list automatically
  6. New domains (e.g. "monitoring-infra") appear without code deploy

Java equivalent flow:
  PatternClusterer.clusterPatterns() → DynamicPatternConfigService.updateDomainConfig()
  → PatternConfigUpdatedEvent → PatternsReloadedEvent → domain-definitions.yml updated
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from backend.services import storage_service as storage_service
from backend.services import taxonomy_discovery

router = APIRouter(prefix="/api/taxonomy", tags=["taxonomy"])


class ApproveRequest(BaseModel):
    approved_names: dict[str, str]  # {"cluster_0": "monitoring-infra"}
    quality_threshold: float = 0.0  # reject if silhouette below this


class AddDomainRequest(BaseModel):
    domain_id: str                   # "monitoring-infra"
    label: str                       # "Monitoring & Observability"
    tech_signals: list[str] = []     # ["Prometheus", "Grafana", "InfluxDB"]
    primary_languages: list[str] = []
    parent_domain: Optional[str] = None
    notes: Optional[str] = None


# ── Clustering ────────────────────────────────────────────────────────────────

@router.post("/discover")
async def discover_taxonomy(
    method: str = "dbscan",
    eps: float = 0.25,
    min_samples: int = 2,
    n_clusters: int = 8
):
    """
    Run DBSCAN clustering on embedded analyses to discover natural domain groups.

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
    Store human-approved cluster names as active domain taxonomy.

    After POST /discover, review cluster summaries and submit approved names.
    Only explicitly approved clusters become domains.

    Java equivalent: DynamicPatternConfigService.updateDomainConfig()
    which writes to domain-definitions.yml and publishes PatternConfigUpdatedEvent.
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


# ── Domain CRUD ───────────────────────────────────────────────────────────────

@router.get("/domains")
async def list_domains():
    """
    Return all active domains — used by:
      1. Frontend dropdown (instead of hardcoded list)
      2. ai_pipeline_rag._get_domain_options() for Gemini prompt

    Combines: system defaults + DBSCAN-discovered + manually added.
    Java equivalent: domain-definitions.yml loaded by DomainDefinition.java
    via DynamicPatternConfigService.
    """
    domains = await storage_service.get_all_domains()

    if not domains:
        # Return defaults when MongoDB domains collection is empty
        defaults = [
                {"domain_id": "web_api",      "label": "Web API",              "source": "default"},
                {"domain_id": "web_app",      "label": "Web Application",      "source": "default"},
                {"domain_id": "cli_tool",     "label": "CLI Tool",             "source": "default"},
                {"domain_id": "library",      "label": "Library / Framework",  "source": "default"},
                {"domain_id": "database",     "label": "Database / Storage",   "source": "default"},
                {"domain_id": "data_pipeline","label": "Data Pipeline",        "source": "default"},
                {"domain_id": "ml_platform",  "label": "ML Platform",          "source": "default"},
                {"domain_id": "infra_tool",   "label": "Infrastructure Tool",  "source": "default"},
                {"domain_id": "mobile_app",   "label": "Mobile App",           "source": "default"},
                {"domain_id": "desktop_app",  "label": "Desktop App",          "source": "default"},
                {"domain_id": "language",     "label": "Programming Language", "source": "default"},
                {"domain_id": "unknown",      "label": "Unknown",              "source": "default"},
        ]
        return {"domains": defaults, "total": len(defaults), "source": "defaults"}

    return {
        "domains":      domains,
        "total":        len(domains),
        "has_emergent": any(d.get("source") == "emergent_clustering" for d in domains),
        "has_manual":   any(d.get("source") == "manual" for d in domains),
    }


@router.post("/domains")
async def add_domain(request: AddDomainRequest):
    """
    Manually add a domain not discovered by clustering.
    Use for edge cases: "blockchain", "embedded", "game-engine", "desktop-app".
    Java equivalent: manually editing domain-definitions.yml.
    """
    doc = {
        "domain_id":        request.domain_id,
        "label":            request.label,
        "tech_signals":     request.tech_signals,
        "primary_languages": request.primary_languages,
        "parent_domain":    request.parent_domain,
        "notes":            request.notes,
        "source":           "manual",
        "usage_count":      0,
        "created_at":       datetime.utcnow().isoformat(),
        "status":           "active"
    }
    await storage_service.store_domain(request.domain_id, doc)
    return {"created": True, "domain_id": request.domain_id}


@router.delete("/domains/{domain_id}")
async def remove_domain(domain_id: str):
    """Soft delete — marks inactive, preserves history."""
    await storage_service.delete_domain(domain_id)
    return {"deleted": True, "domain_id": domain_id}



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
async def get_domain_prompt_fragment():
    """
    Returns the domain classification string injected into Gemini prompt.
    ai_pipeline_rag._get_domain_options() calls this internally.

    Example output:
      "web_api | data_pipeline | ml_platform | ... | monitoring-infra | unknown"

    New domains discovered by DBSCAN appear here automatically after approval.
    Java equivalent: DomainDefinition.getAllDomainIds() used by GeminiServiceImpl.
    """
    domains = await storage_service.get_all_domains()

    if not domains:
        domain_ids = [
            "web_api", "data_pipeline", "ml_platform", "microservice",
            "fullstack", "cli_tool", "library", "infra", "unknown"
        ]
    else:
        domain_ids = [
            d["domain_id"] for d in domains
            if d.get("status", "active") == "active"
        ]
        if "unknown" not in domain_ids:
            domain_ids.append("unknown")

    return {
        "fragment":     " | ".join(domain_ids),
        "domain_count": len(domain_ids),
        "domains":      domain_ids
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
                    "domain":      metadata[i]["current_domain"],
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