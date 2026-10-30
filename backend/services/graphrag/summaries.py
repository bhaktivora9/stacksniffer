"""Hierarchical community summaries with cited evidence (SUMMARIZING).

A leaf community is summarized from a bounded evidence pack: its most connected declarations,
each with its definition evidence and a short excerpt, labelled [E1], [E2], ... A parent
community is summarized from its children's summaries, labelled [C1], [C2], ... The model must
cite labels for every key point; labels it was not given are dropped and counted, a point left
without a valid citation is unsupported, and a summary with no supported point is UNSUPPORTED.

Summaries are versioned by schema, prompt, model profile and input fingerprint, so an unchanged
community reuses its READY summary. A provider failure marks that summary FAILED and fails the
stage after every community was attempted: a partial set is never published as complete, and a
retry regenerates only what is missing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .communities import Community, CommunityRun
from .llm import GenerationError, TextProvider
from .projection import ProjectedGraph

logger = logging.getLogger(__name__)

SUMMARY_SCHEMA_VERSION = "community-summary/1"
PROMPT_VERSION = "community-summary-prompt/1"
PACK_ITEMS = 12
EXCERPT_CHARS = 700
MAX_FINE_SUMMARIES = 40
MIN_FINE_MEMBERS = 4
_PRIORITY = {"CLASS": 0, "INTERFACE": 0, "FUNCTION": 1, "METHOD": 2, "FILE": 3}
_REF = re.compile(r"^(E|C)\d+$")

SYSTEM = (
    "You document a software repository for engineers who are new to it. You are given one group of "
    "related code, found by clustering its dependency graph. Describe what the group does and how its "
    "parts work together, using only the items provided. Every key point must cite the labels of the "
    "items that support it, exactly as given (for example E2 or C1). Never invent labels, names or behaviour.")

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "A short name for the component, 2-6 words."},
        "summary": {"type": "string", "description": "2-4 sentences: purpose, and how the parts interact."},
        "key_points": {
            "type": "array", "minItems": 1, "maxItems": 6,
            "items": {"type": "object", "properties": {
                "text": {"type": "string"},
                "references": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                "required": ["text", "references"]}},
    },
    "required": ["title", "summary", "key_points"],
}


class SummaryGenerationIncomplete(RuntimeError):
    failure_code = "SUMMARIES_INCOMPLETE"


@dataclass
class _Pack:
    community: Community
    prompt: str
    refs: dict[str, dict]  # label -> {"evidence_ids": [...], ...display fields}
    fingerprint: str = ""

    def __post_init__(self):
        self.fingerprint = hashlib.sha256(f"{SUMMARY_SCHEMA_VERSION}|{PROMPT_VERSION}|{self.prompt}".encode()).hexdigest()


@dataclass
class SummaryResult:
    ready: int = 0
    reused: int = 0
    unsupported: int = 0
    failed: int = 0
    skipped: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failures: list[str] = field(default_factory=list)


def selected_communities(run: CommunityRun) -> tuple[list[Community], list[Community]]:
    """(fine communities to summarize from members, top-level communities)."""
    fine = sorted((c for c in run.communities if c.level > 0 and len(c.members) >= MIN_FINE_MEMBERS),
                  key=lambda c: (-len(c.members), c.key))[:MAX_FINE_SUMMARIES]
    return fine, [c for c in run.communities if c.level == 0]


def summarize_communities(connection_factory: Callable[[], Any], run: CommunityRun, graph: ProjectedGraph,
                          provider: TextProvider, *, repository: str, workers: int = 4) -> SummaryResult:
    result = SummaryResult()
    fine, top = selected_communities(run)
    chosen_fine = {c.id for c in fine}
    summaries: dict[UUID, dict] = {}  # community id -> structured summary (for parents)

    def run_batch(packs: list[_Pack]) -> None:
        todo = []
        for pack in packs:
            existing = _with_conn(connection_factory, lambda conn: _existing(conn, pack, provider))
            if existing is not None:
                result.reused += 1
                summaries[pack.community.id] = existing
            else:
                todo.append(pack)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            outcomes = list(pool.map(lambda pack: _generate(pack, provider), todo))
        for pack, outcome in zip(todo, outcomes):
            stored = _with_conn(connection_factory, lambda conn: _store(conn, pack, provider, outcome, result))
            if stored is not None:
                summaries[pack.community.id] = stored

    leaf_packs = _with_conn(connection_factory, lambda conn: [
        _member_pack(conn, c, graph, repository) for c in fine])
    run_batch(leaf_packs)
    parent_packs = []
    for community in top:
        children = [c for c in run.children(community.id) if c.id in chosen_fine and c.id in summaries]
        if children:
            parent_packs.append(_children_pack(community, children, summaries, repository))
        else:
            parent_packs.append(_with_conn(connection_factory,
                                           lambda conn: _member_pack(conn, community, graph, repository)))
    run_batch(parent_packs)
    result.skipped = len(run.communities) - len(fine) - len(top)
    if result.failed:
        raise SummaryGenerationIncomplete(
            f"{result.failed} community summaries failed ({'; '.join(sorted(set(result.failures))[:3])}); "
            f"{result.ready + result.reused} are ready and will be reused on retry")
    return result


def _with_conn(connection_factory: Callable[[], Any], work: Callable[[Any], Any]) -> Any:
    conn = connection_factory()
    try:
        value = work(conn)
        conn.commit()
        return value
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _member_pack(conn: Any, community: Community, graph: ProjectedGraph, repository: str) -> _Pack:
    members = set(community.members)
    degree = {m: sum(1 for n, _, _ in graph.neighbors(m) if n in members) for m in members}
    ranked = sorted(members, key=lambda m: (_PRIORITY.get(graph.nodes[m].entity_type, 4), -degree[m],
                                            graph.nodes[m].stable_key))
    declarations = [m for m in ranked if graph.nodes[m].entity_type != "FILE"] or ranked
    chosen = declarations[:PACK_ITEMS]
    evidence = definition_evidence(conn, chosen)
    excerpts = _excerpts(conn, chosen)
    refs, lines = {}, [f"Repository: {repository}", f"Code group with {len(members)} entities. Items:"]
    for number, entity_id in enumerate((m for m in chosen if m in evidence), start=1):
        node = graph.nodes[entity_id]
        label = f"E{number}"
        location = f"{node.path}:{node.start_line}-{node.end_line}" if node.start_line else node.path
        refs[label] = {"kind": "entity", "entity_id": str(entity_id), "stable_key": node.stable_key,
                       "evidence_ids": [str(evidence[entity_id])], "file": node.path, "start_line": node.start_line,
                       "end_line": node.end_line}
        lines.append(f"\n[{label}] {node.entity_type} {node.qualified_name or node.name} ({location})")
        if entity_id in excerpts:
            lines.append("```\n" + excerpts[entity_id][:EXCERPT_CHARS] + "\n```")
    return _Pack(community, "\n".join(lines), refs)


def _children_pack(community: Community, children: list[Community], summaries: dict[UUID, dict],
                   repository: str) -> _Pack:
    refs, lines = {}, [f"Repository: {repository}",
                       f"A larger component made of {len(children)} sub-components. Their summaries:"]
    for number, child in enumerate(children, start=1):
        label = f"C{number}"
        summary = summaries[child.id]
        refs[label] = {"kind": "community", "community_id": str(child.id), "title": summary.get("title"),
                       "evidence_ids": summary.get("evidence_ids", [])}
        lines.append(f"\n[{label}] {summary.get('title')}: {summary.get('summary')}")
        for point in summary.get("key_points", [])[:4]:
            lines.append(f"  - {point.get('text')}")
    return _Pack(community, "\n".join(lines), refs)


def definition_evidence(conn: Any, entity_ids: list[UUID]) -> dict[UUID, UUID]:
    rows = conn.fetch_all(
        "SELECT DISTINCT ON (ee.entity_id) ee.entity_id, ee.evidence_id FROM core.entity_evidence ee "
        "JOIN core.evidence ev ON ev.id = ee.evidence_id WHERE ee.entity_id = ANY(%s::uuid[]) "
        "ORDER BY ee.entity_id, (ee.evidence_role = 'DEFINITION') DESC, ev.start_line",
        [str(e) for e in entity_ids])
    return {entity_id: evidence_id for entity_id, evidence_id in rows}


def _excerpts(conn: Any, entity_ids: list[UUID]) -> dict[UUID, str]:
    rows = conn.fetch_all(
        "SELECT DISTINCT ON (c.entity_id) c.entity_id, c.content FROM semantic.chunk c "
        "WHERE c.entity_id = ANY(%s::uuid[]) ORDER BY c.entity_id, c.ordinal", [str(e) for e in entity_ids])
    return {entity_id: content for entity_id, content in rows}


def _existing(conn: Any, pack: _Pack, provider: TextProvider) -> dict | None:
    row = conn.fetch_one(
        "SELECT structured_summary FROM graphrag.community_summary WHERE community_id = %s "
        "AND summary_schema_version = %s AND prompt_version = %s AND model_profile = %s AND input_fingerprint = %s "
        "AND status IN ('READY', 'UNSUPPORTED')",
        str(pack.community.id), SUMMARY_SCHEMA_VERSION, PROMPT_VERSION, provider.profile, pack.fingerprint)
    return row[0] if row else None


def _generate(pack: _Pack, provider: TextProvider):
    if not pack.refs:
        return ValueError("no evidence to summarize")
    try:
        return provider.generate_json(SYSTEM, pack.prompt, SCHEMA)
    except GenerationError as exc:
        return exc


def _store(conn: Any, pack: _Pack, provider: TextProvider, outcome: Any, result: SummaryResult) -> dict | None:
    conn.execute(
        "DELETE FROM graphrag.community_summary WHERE community_id = %s AND summary_schema_version = %s "
        "AND prompt_version = %s AND model_profile = %s AND input_fingerprint = %s",
        str(pack.community.id), SUMMARY_SCHEMA_VERSION, PROMPT_VERSION, provider.profile, pack.fingerprint)
    if isinstance(outcome, Exception):
        if isinstance(outcome, ValueError):  # nothing citable: not a provider failure
            result.unsupported += 1
            status, text, structured = "UNSUPPORTED", None, {"reason": str(outcome)}
        else:
            result.failed += 1
            result.failures.append(getattr(outcome, "failure_code", type(outcome).__name__))
            logger.warning("Summary of community %s failed: %s", pack.community.key, outcome)
            status, text, structured = "FAILED", None, {"error": getattr(outcome, "failure_code", "ERROR")}
        conn.execute(
            "INSERT INTO graphrag.community_summary (community_id, summary_schema_version, prompt_version, "
            "model_profile, input_fingerprint, summary_text, structured_summary, status, completed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, now())",
            str(pack.community.id), SUMMARY_SCHEMA_VERSION, PROMPT_VERSION, provider.profile, pack.fingerprint,
            text, json.dumps(structured), status)
        return None

    result.input_tokens += outcome.input_tokens
    result.output_tokens += outcome.output_tokens
    data = outcome.data
    points, invalid = [], 0
    for point in data.get("key_points") or []:
        labels = [r for r in dict.fromkeys(point.get("references") or []) if isinstance(r, str) and _REF.match(r)]
        valid = [label for label in labels if label in pack.refs]
        invalid += len(labels) - len(valid)
        text = (point.get("text") or "").strip()
        if text:
            points.append({"text": text, "references": valid, "supported": bool(valid)})
    supported = [p for p in points if p["supported"]]
    title = (data.get("title") or "").strip() or pack.community.key
    summary = (data.get("summary") or "").strip()
    evidence_ids = sorted({e for p in supported for label in p["references"] for e in pack.refs[label]["evidence_ids"]})
    structured = {"title": title, "summary": summary, "key_points": points, "references": pack.refs,
                  "invalid_references": invalid, "unsupported_points": len(points) - len(supported),
                  "evidence_ids": evidence_ids, "member_count": len(pack.community.members)}
    status = "READY" if supported else "UNSUPPORTED"
    text = "\n".join([title, summary] + [f"- {p['text']}" for p in supported]) if supported else None
    summary_id = conn.fetch_scalar(
        "INSERT INTO graphrag.community_summary (community_id, summary_schema_version, prompt_version, model_profile, "
        "input_fingerprint, summary_text, structured_summary, status, completed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, now()) RETURNING id",
        str(pack.community.id), SUMMARY_SCHEMA_VERSION, PROMPT_VERSION, provider.profile, pack.fingerprint, text,
        json.dumps(structured), status)
    for number, point in enumerate(supported, start=1):
        for evidence_id in sorted({e for label in point["references"] for e in pack.refs[label]["evidence_ids"]})[:8]:
            conn.execute("INSERT INTO graphrag.community_summary_citation (summary_id, evidence_id, claim_key) "
                         "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", str(summary_id), evidence_id, f"p{number}")
    if status == "READY":
        result.ready += 1
    else:
        result.unsupported += 1
    return structured
