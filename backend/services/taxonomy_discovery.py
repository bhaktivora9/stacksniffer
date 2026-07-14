"""
backend/services/taxonomy_discovery.py

Emergent taxonomy discovery via embedding clustering.
Python prototype for stacksniffer-learning PatternClusterer.java

Flow:
  1. Load all embeddings from MongoDB
  2. Run DBSCAN (density-based) — discovers natural clusters without predefining count
  3. Run KMeans as comparison — forces fixed cluster count
  4. Human reviews cluster summaries and assigns domain names
  5. Named clusters become the new domain taxonomy
  6. Store in MongoDB domains collection

DBSCAN chosen over KMeans because:
  - Stack distributions are not spherical (KMeans assumption violated)
  - Number of domains is unknown — DBSCAN discovers it
  - Noise points (genuinely ambiguous repos) are labeled -1, not forced into a cluster
  - Maps directly to PatternClusterer.java DBSCAN implementation
"""
import logging
import json
from collections import Counter, defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Phase 1: Load embeddings from MongoDB ─────────────────────────────────────

async def load_embeddings_for_clustering() -> tuple[list, list, list]:
    """
    Load all analyses that have embeddings.
    Returns: (embeddings, analysis_ids, metadata_list)
    """
    from backend.services import storage_service

    analyses = await storage_service.get_all_analyses(with_embeddings_only=True)

    if not analyses:
        return [], [], []

    embeddings = []
    analysis_ids = []
    metadata = []

    for a in analyses:
        emb = a.get("stack_embedding")
        if not emb or len(emb) != 768:
            continue
        embeddings.append(emb)
        analysis_ids.append(a["analysis_id"])
        metadata.append({
            "repo": a.get("repo", {}).get("full_name", "unknown"),
            "current_domain": a.get("stack", {}).get("domain", "unknown"),
            "primary_language": a.get("stack", {}).get("primary_language", ""),
            "frameworks": [
                t["name"] for t in a.get("stack", {}).get("frameworks", [])
            ],
            "ai_ml": [
                t["name"] for t in a.get("stack", {}).get("ai_ml", [])
            ],
            "complexity": a.get("stack", {}).get("complexity_score", 0),
            "stack_pattern": a.get("stack", {}).get("stack_pattern", ""),
        })

    logger.info("Loaded %d embeddings for clustering", len(embeddings))
    return embeddings, analysis_ids, metadata


# ── Phase 2: Cluster with DBSCAN + KMeans ─────────────────────────────────────

def cluster_dbscan(
    embeddings: list,
    eps: float = 0.25,
    min_samples: int = 2
) -> list[int]:
    """
    DBSCAN clustering — discovers natural cluster count.
    eps: cosine distance threshold (0.25 = clusters with >75% similarity)
    min_samples: minimum repos to form a cluster (2 for small corpus)

    Returns list of cluster labels (-1 = noise/outlier)

    eps tuning guide:
      0.15 → tight clusters, many outliers, high purity
      0.25 → balanced (recommended for 20-100 repos)
      0.40 → loose clusters, few outliers, lower purity
    """
    try:
        import numpy as np
        from sklearn.cluster import DBSCAN
        from sklearn.preprocessing import normalize

        X = normalize(np.array(embeddings))  # L2 normalize for cosine similarity
        db = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine")
        labels = db.fit_predict(X)
        return labels.tolist()
    except ImportError:
        raise ImportError("Run: pip install scikit-learn numpy")


def cluster_kmeans(
    embeddings: list,
    n_clusters: int = 8
) -> tuple[list[int], float]:
    """
    KMeans clustering — forces fixed cluster count.
    Use as comparison against DBSCAN.
    Returns: (labels, inertia)
    """
    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import normalize

        X = normalize(np.array(embeddings))
        km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        labels = km.fit_predict(X)
        return labels.tolist(), float(km.inertia_)
    except ImportError:
        raise ImportError("Run: pip install scikit-learn numpy")


def find_optimal_k(embeddings: list, k_range: range = range(3, 15)) -> dict:
    """
    Elbow method to find optimal KMeans K.
    Plot inertia vs K — the elbow is the optimal cluster count.
    """
    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import normalize

        X = normalize(np.array(embeddings))
        results = {}
        for k in k_range:
            if k >= len(embeddings):
                break
            km = KMeans(n_clusters=k, random_state=42, n_init=10)
            km.fit(X)
            results[k] = round(float(km.inertia_), 4)
        return results
    except ImportError:
        raise ImportError("Run: pip install scikit-learn numpy")


# ── Phase 3: Interpret clusters ───────────────────────────────────────────────

def summarize_clusters(
    labels: list[int],
    metadata: list[dict],
    analysis_ids: list[str]
) -> dict:
    """
    For each cluster: summarize what repos are in it and what they have in common.
    This is the human-readable output for naming the clusters.
    """
    clusters = defaultdict(list)
    for i, label in enumerate(labels):
        clusters[label].append({
            "idx": i,
            "analysis_id": analysis_ids[i],
            **metadata[i]
        })

    summary = {}
    for cluster_id, members in clusters.items():
        if cluster_id == -1:
            label = "noise_outliers"
        else:
            label = f"cluster_{cluster_id}"

        # Aggregate signals across cluster members
        all_frameworks = []
        all_ai_ml = []
        all_languages = []
        all_patterns = []
        current_domains = []

        for m in members:
            all_frameworks.extend(m.get("frameworks", []))
            all_ai_ml.extend(m.get("ai_ml", []))
            all_languages.append(m.get("primary_language", ""))
            if m.get("stack_pattern"):
                all_patterns.append(m["stack_pattern"])
            current_domains.append(m.get("current_domain", "unknown"))

        # Most common signals
        top_frameworks = [f for f, _ in Counter(all_frameworks).most_common(5)]
        top_languages  = [l for l, _ in Counter(all_languages).most_common(3) if l]
        top_ai_ml      = [a for a, _ in Counter(all_ai_ml).most_common(3)]
        top_patterns   = [p for p, _ in Counter(all_patterns).most_common(2)]
        domain_votes   = Counter(current_domains).most_common(3)

        avg_complexity = round(
            sum(m.get("complexity", 0) for m in members) / max(len(members), 1), 1
        )

        summary[label] = {
            "cluster_id": cluster_id,
            "size": len(members),
            "repos": [m["repo"] for m in members],
            "analysis_ids": [m["analysis_id"] for m in members],
            "signals": {
                "top_frameworks": top_frameworks,
                "top_languages":  top_languages,
                "top_ai_ml":      top_ai_ml,
                "top_patterns":   top_patterns,
                "avg_complexity": avg_complexity,
            },
            "current_domain_votes": dict(domain_votes),
            "suggested_name": _suggest_cluster_name(
                top_frameworks, top_languages, top_ai_ml, top_patterns
            ),
            "needs_human_review": cluster_id != -1,
        }

    return summary


def _suggest_cluster_name(
    frameworks: list,
    languages: list,
    ai_ml: list,
    patterns: list
) -> str:
    """
    Heuristic name suggestion for a cluster.
    Human should review and override — this is a starting point only.
    """
    fw_lower = [f.lower() for f in frameworks]
    ai_lower = [a.lower() for a in ai_ml]
    lang_lower = [l.lower() for l in languages]

    # AI/ML signals — strongest indicator
    if len(ai_ml) >= 2 or any(x in ai_lower for x in ["langchain", "openai", "gemini", "vertex ai"]):
        if any(x in ai_lower for x in ["langchain", "llamaindex", "autogen"]):
            return "llm-application-platform"
        return "ml-platform"

    # Data signals
    if any(x in fw_lower for x in ["kafka", "spark", "airflow", "dagster", "prefect"]):
        return "data-pipeline"

    # Infrastructure signals
    if "go" in lang_lower and not any(x in fw_lower for x in ["gin", "echo", "fiber"]):
        return "infrastructure-tooling"

    # Web API signals
    if any(x in fw_lower for x in ["fastapi", "django", "spring boot", "express", "nestjs"]):
        # Check for frontend too
        if any(x in fw_lower for x in ["react", "next.js", "vue", "angular"]):
            return "fullstack-web-application"
        return "web-api-service"

    # Frontend only
    if any(x in fw_lower for x in ["react", "next.js", "vue"]) and "go" not in lang_lower:
        return "frontend-application"

    # Library signals
    if "custom" in [p.lower() for p in patterns] or not frameworks:
        return "library-or-sdk"

    return "unclassified"


# ── Phase 4: Validate cluster quality ────────────────────────────────────────

def compute_cluster_quality(
    embeddings: list,
    labels: list[int]
) -> dict:
    """
    Silhouette score — measures cluster cohesion and separation.
    Range: -1 (bad) to +1 (good). Above 0.3 is acceptable.

    This is the key validation step before trusting the discovered taxonomy.
    If silhouette < 0.2 — embeddings don't capture stack similarity well enough.
    Fix: improve build_stack_fingerprint() to include more semantic signals.
    """
    try:
        import numpy as np
        from sklearn.metrics import silhouette_score, davies_bouldin_score
        from sklearn.preprocessing import normalize

        X = normalize(np.array(embeddings))
        non_noise = [(i, l) for i, l in enumerate(labels) if l != -1]

        if len(non_noise) < 4 or len(set(l for _, l in non_noise)) < 2:
            return {
                "error": "Not enough clustered points for quality metrics",
                "recommendation": "Lower eps in DBSCAN or collect more repos"
            }

        valid_idx = [i for i, _ in non_noise]
        valid_labels = [l for _, l in non_noise]

        X_valid = X[valid_idx]
        sil = float(silhouette_score(X_valid, valid_labels, metric="cosine"))
        db_score = float(davies_bouldin_score(X_valid, valid_labels))

        noise_ratio = labels.count(-1) / max(len(labels), 1)
        n_clusters = len(set(l for l in labels if l != -1))

        quality = "poor"
        if sil > 0.5:
            quality = "excellent"
        elif sil > 0.35:
            quality = "good"
        elif sil > 0.2:
            quality = "acceptable"

        return {
            "silhouette_score": round(sil, 4),
            "davies_bouldin_score": round(db_score, 4),
            "n_clusters_discovered": n_clusters,
            "noise_ratio": round(noise_ratio, 3),
            "quality": quality,
            "interpretation": {
                "silhouette": f"{sil:.3f} ({quality}) — {'embeddings capture stack similarity well' if sil > 0.3 else 'embeddings may not capture stack semantics — improve fingerprint'}",
                "noise_ratio": f"{noise_ratio:.0%} of repos are outliers — {'normal' if noise_ratio < 0.2 else 'high — lower eps or collect more similar repos'}",
                "recommendation": (
                    "Taxonomy is trustworthy — proceed to naming clusters"
                    if sil > 0.3 else
                    "Silhouette too low — improve embedding fingerprint before trusting taxonomy"
                )
            }
        }
    except ImportError:
        return {"error": "pip install scikit-learn"}


# ── Phase 5: Store discovered taxonomy ───────────────────────────────────────

async def store_discovered_taxonomy(
    cluster_summary: dict,
    approved_names: dict[str, str],  # {cluster_label: human_approved_name}
    quality_metrics: dict
) -> dict:
    """
    Store human-approved cluster names as the new domain taxonomy.
    Only stores clusters that have been explicitly named by human review.

    approved_names example:
    {
        "cluster_0": "llm-application-platform",
        "cluster_1": "data-pipeline",
        "cluster_2": "web-api-service",
        "cluster_3": "infrastructure-tooling"
    }
    """
    from backend.services import storage_service

    stored = []
    for cluster_label, domain_name in approved_names.items():
        if cluster_label not in cluster_summary:
            continue

        cluster = cluster_summary[cluster_label]
        doc = {
            "domain_id": domain_name,
            "label": domain_name.replace("-", " ").title(),
            "source": "emergent_clustering",
            "cluster_id": cluster["cluster_id"],
            "size_at_discovery": cluster["size"],
            "member_repos": cluster["repos"],
            "tech_signals": cluster["signals"]["top_frameworks"] +
                            cluster["signals"]["top_ai_ml"],
            "primary_languages": cluster["signals"]["top_languages"],
            "quality_score": quality_metrics.get("silhouette_score", 0),
            "usage_count": cluster["size"],
            "created_at": datetime.utcnow().isoformat(),
            "status": "active"
        }
        await storage_service.store_domain(domain_name, doc)
        stored.append(domain_name)

    return {
        "domains_stored": len(stored),
        "domain_ids": stored,
        "message": (
            f"Stored {len(stored)} emergent domains. "
            "Update Gemini prompt and frontend dropdown to use /api/domains endpoint."
        )
    }


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def discover_taxonomy(
    method: str = "dbscan",
    eps: float = 0.25,
    min_samples: int = 2,
    n_clusters: int = 8
) -> dict:
    """
    Full taxonomy discovery pipeline.
    Returns cluster summaries for human review — does NOT auto-approve names.
    Human must call store_discovered_taxonomy() with approved_names.
    """
    # Step 1: Load embeddings
    embeddings, analysis_ids, metadata = await load_embeddings_for_clustering()

    if len(embeddings) < 10:
        return {
            "status": "insufficient_data",
            "current_count": len(embeddings),
            "needed": 10,
            "message": (
                f"Need at least 10 embedded analyses for meaningful clustering. "
                f"Currently have {len(embeddings)}. Run seed_corpus.py first."
            )
        }

    # Step 2: Cluster
    if method == "dbscan":
        labels = cluster_dbscan(embeddings, eps=eps, min_samples=min_samples)
        method_params = {"eps": eps, "min_samples": min_samples}
    else:
        labels, inertia = cluster_kmeans(embeddings, n_clusters=n_clusters)
        method_params = {"n_clusters": n_clusters, "inertia": inertia}

    # Step 3: Quality check
    quality = compute_cluster_quality(embeddings, labels)

    # Step 4: Summarize
    summary = summarize_clusters(labels, metadata, analysis_ids)

    # Step 5: Elbow analysis (always run for reference)
    elbow = {}
    try:
        if len(embeddings) >= 6:
            elbow = find_optimal_k(embeddings, range(2, min(len(embeddings)//2, 12)))
    except Exception:
        pass

    n_clusters_found = len(set(l for l in labels if l != -1))
    noise_count = labels.count(-1)

    return {
        "status": "review_required",
        "method": method,
        "method_params": method_params,
        "total_repos": len(embeddings),
        "clusters_discovered": n_clusters_found,
        "noise_outliers": noise_count,
        "quality": quality,
        "elbow_analysis": elbow,
        "clusters": summary,
        "next_step": (
            "Review cluster summaries. "
            "Call POST /api/taxonomy/approve with approved_names dict. "
            "Each cluster's 'suggested_name' is a starting point — override as needed."
        )
    }