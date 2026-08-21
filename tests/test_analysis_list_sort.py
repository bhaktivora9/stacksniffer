import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from routers import analyze


DOCS = [
    {
        "repo_key": "github:owner/zulu",
        "repo_metadata": {"full_name": "owner/Zulu"},
        "updated_at": "2026-01-01T00:00:00Z",
        "stack": {
            "software_type": "service",
            "software_type_ai": "web_api",
            "software_type_ai_reasoning": "AI matched API service evidence",
            "ai_classification_used": True,
            "specific_identity": "api_server",
            "layer0_prediction": {"software_type": "library"},
        },
    },
    {
        "repo_key": "github:owner/alpha",
        "repo_metadata": {"full_name": "owner/Alpha"},
        "updated_at": "2026-02-01T00:00:00Z",
        "stack": {"software_type": "library"},
    },
]


@pytest.mark.parametrize(
    ("sort_by", "sort_order", "expected"),
    [
        ("time", "desc", ["owner/Alpha", "owner/Zulu"]),
        ("time", "asc", ["owner/Zulu", "owner/Alpha"]),
        ("name", "asc", ["owner/Alpha", "owner/Zulu"]),
        ("name", "desc", ["owner/Zulu", "owner/Alpha"]),
    ],
)
def test_list_analyses_sorting(monkeypatch, sort_by, sort_order, expected):
    async def get_all_analyses():
        return DOCS

    monkeypatch.setattr(analyze.storage_service, "get_all_analyses", get_all_analyses)
    async def get_all_feedback():
        return [{"repo_key": "github:owner/alpha"}, {"repo_key": "github:owner/alpha"}]

    monkeypatch.setattr(analyze.storage_service, "get_all_feedback", get_all_feedback)
    result = asyncio.run(
        analyze.list_analyses(limit=100, sort_by=sort_by, sort_order=sort_order)
    )

    assert [row["repo"]["full_name"] for row in result["analyses"]] == expected
    counts = {row["repo"]["full_name"]: row["feedback_count"] for row in result["analyses"]}
    assert counts == {"owner/Alpha": 2, "owner/Zulu": 0}
    zulu = next(row for row in result["analyses"] if row["repo"]["full_name"] == "owner/Zulu")
    assert zulu["layer0_software_type"] == "library"
    assert zulu["ai_software_type"] == "web_api"
    assert zulu["ai_classification_used"] is True
    assert zulu["software_type_ai_reasoning"] == "AI matched API service evidence"
    assert zulu["specific_identity"] == "api_server"
    alpha = next(row for row in result["analyses"] if row["repo"]["full_name"] == "owner/Alpha")
    assert alpha["ai_software_type"] is None
    assert alpha["ai_classification_used"] is False
