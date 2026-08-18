from services.rag_filter import diagnose_rag_context, format_rag_context


def test_rag_filter_drops_non_teaching_and_weak_neighbors():
    neighbors = [
        {
            "repo": {"full_name": "elastic/elasticsearch"},
            "stack": {"software_type": "database", "primary_language": "Java"},
            "score": 0.91,
        },
        {
            "repo": {"full_name": "example/unknown"},
            "stack": {"software_type": "unknown"},
            "score": 0.88,
        },
        {
            "repo": {"full_name": "example/weak"},
            "stack": {"software_type": "library"},
            "score": 0.25,
        },
    ]

    diagnostic = diagnose_rag_context(neighbors)

    assert diagnostic == {
        "total_neighbors": 3,
        "usable_after_filter": 1,
        "dropped_non_teaching_software_type": 1,
        "dropped_below_similarity_floor": 1,
        "surviving_software_types": ["database"],
        "surviving_repos": ["elastic/elasticsearch"],
    }
    context = format_rag_context(neighbors)
    assert "elastic/elasticsearch" in context
    assert "example/unknown" not in context
    assert "example/weak" not in context


def test_rag_filter_prefers_human_corrected_software_type():
    context = format_rag_context([{
        "repo": {"full_name": "elastic/elasticsearch"},
        "stack": {"software_type": "library"},
        "corrections": {"software_type": "database"},
        "score": 0.9,
    }])

    assert "software_type: database" in context
    assert "software_type: library" not in context
