from backend.services.dep_classifier import apply_file_signals


def _by_name(file_tree: list[str]) -> dict[str, dict]:
    return {item["name"]: item for item in apply_file_signals(file_tree)}


def test_common_language_extension_signals():
    detected = _by_name([
        "backend/app.py",
        "backend/routes.py",
        "web/index.js",
        "web/config.js",
        "web/App.tsx",
        "web/Button.tsx",
    ])

    assert detected["Python"]["file_count"] == 2
    assert detected["JavaScript"]["file_count"] == 2
    assert detected["TypeScript"]["file_count"] == 2


def test_related_extensions_are_aggregated_for_language_count():
    detected = _by_name([
        "src/index.ts",
        "src/api.ts",
        "src/App.tsx",
        "src/Button.tsx",
    ])

    assert detected["TypeScript"]["file_count"] == 4


def test_manifest_signal_preserves_and_receives_source_count():
    detected = _by_name(["go.mod", "cmd/main.go", "pkg/service.go"])

    assert detected["Go"]["confidence"] == 1.0
    assert detected["Go"]["file_count"] == 2
