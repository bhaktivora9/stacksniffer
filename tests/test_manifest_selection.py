from backend.services.manifest_parser import select_manifests


def test_namesake_manifest_jumps_partial_coverage_budget():
    tree = ["package.json"]
    tree.extend(f"packages/pkg-{index}/package.json" for index in range(30))
    tree.append("packages/next/package.json")

    manifests, flags = select_manifests(tree, limit=5, repo_name="next.js")
    parsed = {item["path"] for item in manifests if item["parsed"]}
    assert "PARTIAL_MANIFEST_COVERAGE" in flags
    assert "package.json" in parsed
    assert "packages/next/package.json" in parsed
    assert len(parsed) <= 5
