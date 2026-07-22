from backend.services.rag_filter import diagnose_rag_context, format_rag_context


def test_rag_filter_drops_non_teaching_and_weak_neighbors():
    neighbors = [
        {
            "repo": {"full_name": "elastic/elasticsearch"},
            "stack": {"domain": "database", "primary_language": "Java"},
            "score": 0.91,
        },
        {
            "repo": {"full_name": "example/unknown"},
            "stack": {"domain": "unknown"},
            "score": 0.88,
        },
        {
            "repo": {"full_name": "example/weak"},
            "stack": {"domain": "library"},
            "score": 0.25,
        },
    ]

    diagnostic = diagnose_rag_context(neighbors)

    assert diagnostic == {
        "total_neighbors": 3,
        "usable_after_filter": 1,
        "dropped_non_teaching_domain": 1,
        "dropped_below_similarity_floor": 1,
        "surviving_domains": ["database"],
        "surviving_repos": ["elastic/elasticsearch"],
    }
    context = format_rag_context(neighbors)
    assert "elastic/elasticsearch" in context
    assert "example/unknown" not in context
    assert "example/weak" not in context


def test_rag_filter_prefers_human_corrected_domain():
    context = format_rag_context([{
        "repo": {"full_name": "elastic/elasticsearch"},
        "stack": {"domain": "library"},
        "corrections": {"domain": "database"},
        "score": 0.9,
    }])

    assert "domain: database" in context
    assert "domain: library" not in context
