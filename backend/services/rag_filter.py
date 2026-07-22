"""Curate RAG neighbors before using them as domain few-shot examples."""

_RAG_SIMILARITY_FLOOR = 0.55
_NON_TEACHING_DOMAINS = {"unknown", "", None}
_MAX_RAG_EXAMPLES = 5


def _neighbor_domain(repo: dict) -> str | None:
    corrected = (repo.get("corrections") or {}).get("domain")
    if corrected and corrected not in _NON_TEACHING_DOMAINS:
        return corrected
    domain = (repo.get("stack") or {}).get("domain")
    if domain and domain not in _NON_TEACHING_DOMAINS:
        return domain
    return None


def _neighbor_name(repo: dict) -> str | None:
    repo_block = repo.get("repo") or {}
    if repo_block.get("full_name"):
        return repo_block["full_name"]
    owner, name = repo.get("owner"), repo.get("name")
    return f"{owner}/{name}" if owner and name else None


def _usable_neighbors(similar_repos: list[dict]) -> list[dict]:
    usable = []
    for repo in similar_repos or []:
        score = repo.get("score", repo.get("similarity", 0.0)) or 0.0
        domain = _neighbor_domain(repo)
        name = _neighbor_name(repo)
        if score < _RAG_SIMILARITY_FLOOR or domain is None or not name:
            continue
        usable.append({
            "name": name,
            "domain": domain,
            "score": score,
            "stack_pattern": (repo.get("stack") or {}).get("stack_pattern"),
            "primary_language": (repo.get("stack") or {}).get("primary_language"),
        })
    usable.sort(key=lambda repo: repo["score"], reverse=True)
    return usable[:_MAX_RAG_EXAMPLES]


def format_rag_context(similar_repos: list[dict]) -> str:
    usable = _usable_neighbors(similar_repos)
    if not usable:
        return (
            "\nNo sufficiently similar, well-labeled repositories were found in "
            "the corpus. Classify this repository on its own merits.\n"
        )

    lines = [
        "\nSIMILAR WELL-LABELED REPOSITORIES (for reference — classify the "
        "TARGET repo, do not just copy these):"
    ]
    for repo in usable:
        language = f" [{repo['primary_language']}]" if repo.get("primary_language") else ""
        pattern = f" · {repo['stack_pattern']}" if repo.get("stack_pattern") else ""
        lines.append(
            f"  • {repo['name']}{language} → domain: {repo['domain']}{pattern} "
            f"(similarity {repo['score']:.2f})"
        )
    lines.append(
        "\nThese are hints, not answers. A repository's domain is determined by "
        "what it DOES, not by which repositories it resembles. If the target "
        "clearly belongs to a domain none of these show, use that domain.\n"
    )
    return "\n".join(lines)


def diagnose_rag_context(similar_repos: list[dict]) -> dict:
    raw = similar_repos or []
    usable = _usable_neighbors(raw)
    return {
        "total_neighbors": len(raw),
        "usable_after_filter": len(usable),
        "dropped_non_teaching_domain": sum(
            1 for repo in raw if _neighbor_domain(repo) is None
        ),
        "dropped_below_similarity_floor": sum(
            1
            for repo in raw
            if (repo.get("score", repo.get("similarity", 0.0)) or 0.0)
            < _RAG_SIMILARITY_FLOOR
        ),
        "surviving_domains": [repo["domain"] for repo in usable],
        "surviving_repos": [repo["name"] for repo in usable],
    }
