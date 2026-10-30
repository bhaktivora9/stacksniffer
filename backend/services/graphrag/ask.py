"""Answer a question about one analysis with local or global GraphRAG, citing its evidence.

LOCAL (a question about specific code): dense seeds from vector search, expanded one hop over
the projected graph (calls, inheritance, the containing class), plus the summaries of the
communities the seeds belong to. GLOBAL (a question about the repository as a whole): community
summaries ranked against the question. Context items are labelled [E1].. (code) and [C1]..
(community summaries); global questions also get a few vector hits, since configuration and
files outside the graph belong to no community. The model must cite labels, and only labels it
was given count. Every run
is persisted in graphrag.query_run with its trace, retrieval items, answer and citations.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from ..semantic.embeddings import EmbeddingProvider
from ..semantic.profiles import ChunkProfile, EmbeddingProfile
from ..semantic.search import SearchError, search_analysis
from .communities import active_run
from .llm import GenerationError, TextProvider
from .projection import ProjectedGraph, active_graph
from .summaries import definition_evidence

RETRIEVER_VERSION = "stacksniffer-graphrag/1"
PROMPT_VERSION = "grounded-answer-prompt/1"
SEEDS = 8
EXPANSION = 6
COMMUNITIES_LOCAL = 2
COMMUNITIES_GLOBAL = 10
GLOBAL_ANCHORS = 4
EXCERPT_CHARS = 1500
_EXPAND = {"CALLS": 1.0, "EXTENDS": 1.5, "IMPLEMENTS": 1.5, "CONTAINS": 0.5}
_GLOBAL_CUES = re.compile(
    r"\b(architecture|overall|overview|organi[sz]ed|structure[ds]?|components?|modules?|layers?|deploy\w*|"
    r"configur\w+|(?:whole|entire) (?:app|application|codebase|repository|project|system)|codebase|"
    r"tech(?:nology)? stack|databases?|schema|dependencies|technolog\w+|feature areas?)\b", re.IGNORECASE)
_LOCAL_CUES = re.compile(r"\b(function|method|class|endpoint|route|where is|which (?:function|method|class))\b",
                         re.IGNORECASE)
# Identifiers in the question (call syntax, snake_case, camelCase, PascalCase) point at specific code.
_IDENTIFIERS = re.compile(r"\b[A-Za-z_]\w*\(|\b[a-z]+_[a-z_]+\b|\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z]+[A-Z]\w*\b")
_LABEL = re.compile(r"\[((?:E|C)\d+)\]")
# Models sometimes group citations as [E2, E7]; they are split into [E2][E7] before validation.
_LABEL_GROUP = re.compile(r"\[\s*((?:E|C)\d+(?:\s*[,;]\s*(?:E|C)\d+)+)\s*\]")
_STOP = frozenset("the a an and or of to in on for with by is are was be how what which where when why who does do "
                  "it its this that these those from into as at can each any all".split())

SYSTEM = (
    "You answer questions about one software repository using only the context provided. Cite the label of "
    "every context item that supports a statement, in square brackets right after the statement, for example "
    "[E2] or [C1]. Use only labels that appear in the context. If the context does not contain enough to "
    "answer, say what is missing and set insufficient_evidence to true. Be concise and concrete; name the "
    "files, classes and functions involved.")
SCHEMA = {"type": "object", "properties": {
    "answer": {"type": "string", "description": "The answer, in Markdown, with [label] citations."},
    "insufficient_evidence": {"type": "boolean"}}, "required": ["answer", "insufficient_evidence"]}


class AskError(RuntimeError):
    failure_code = "ASK_FAILED"
    status_code = 400


class GraphNotReady(AskError):
    failure_code = "GRAPH_NOT_READY"
    status_code = 409


@dataclass
class ContextItem:
    label: str
    kind: Literal["code", "community"]
    text: str
    evidence_ids: list[str]
    source_type: str  # CHUNK, ENTITY or COMMUNITY (graphrag.retrieval_item)
    source_id: str
    score: float
    reason: str
    display: dict = field(default_factory=dict)


@dataclass
class Answer:
    query_run_id: UUID
    mode: str
    answer: str
    grounding_status: str
    citations: list[dict]
    context: list[ContextItem]
    unsupported_labels: list[str]
    latency_ms: int
    input_tokens: int
    output_tokens: int


def route(question: str) -> str:
    """GLOBAL for repository-wide questions, LOCAL for questions about specific code."""
    global_hits = len(_GLOBAL_CUES.findall(question))
    local_hits = len(_LOCAL_CUES.findall(question)) + 2 * len(_IDENTIFIERS.findall(question))
    return "GLOBAL" if global_hits > local_hits else "LOCAL"


def ask(conn: Any, analysis_id: UUID, question: str, *, embedder: EmbeddingProvider, chunk_profile: ChunkProfile,
        embedding_profile: EmbeddingProfile, provider: TextProvider, mode: str | None = None) -> Answer:
    question = question.strip()
    if not question or len(question) > 2000:
        raise AskError("the question must be 1 to 2000 characters")
    requested = mode
    mode = (mode or route(question)).upper()
    if mode not in ("LOCAL", "GLOBAL"):
        raise AskError("mode must be local or global")
    started = time.perf_counter()
    try:
        snapshot, graph = active_graph(conn, analysis_id)
    except Exception as exc:
        raise GraphNotReady(f"analysis {analysis_id} has no usable graph yet: {exc}") from exc
    run = active_run(conn, snapshot.id)
    embedding_profile_id = conn.fetch_scalar("SELECT id FROM semantic.embedding_profile "
                                             "WHERE configuration_fingerprint = %s", embedding_profile.fingerprint)
    if embedding_profile_id is None:
        raise GraphNotReady(f"embedding profile {embedding_profile.profile_key} has no vectors")
    query_run_id = conn.fetch_scalar(
        "INSERT INTO graphrag.query_run (analysis_id, graph_snapshot_id, community_run_id, embedding_profile_id, "
        "query_text, query_type, retrieval_mode, retriever_version, prompt_version, model_profile) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        str(analysis_id), str(snapshot.id), str(run.id) if run else None, str(embedding_profile_id), question, mode,
        f"{mode}_GRAPHRAG", RETRIEVER_VERSION, PROMPT_VERSION, provider.profile)
    trace = _Trace(conn, query_run_id)
    trace.event("route", {"mode": mode, "requested": requested})
    try:
        if mode == "LOCAL":
            context = _local_context(conn, analysis_id, question, graph, run, trace, embedder=embedder,
                                     chunk_profile=chunk_profile, embedding_profile=embedding_profile)
        else:
            context = _global_context(conn, question, run, trace)
            context += _global_anchors(conn, analysis_id, question, graph, trace, embedder=embedder,
                                       chunk_profile=chunk_profile, embedding_profile=embedding_profile)
        _store_retrieval(conn, query_run_id, context)
        if not context:
            answer_text, insufficient, generation = "No relevant code or summaries were found for this question.", True, None
        else:
            prompt = _prompt(question, context)
            began = time.perf_counter()
            generation = provider.generate_json(SYSTEM, prompt, SCHEMA)
            trace.event("generate", {"input_tokens": generation.input_tokens,
                                     "output_tokens": generation.output_tokens}, began)
            answer_text = (generation.data.get("answer") or "").strip() or "The model returned an empty answer."
            answer_text = _LABEL_GROUP.sub(
                lambda m: "".join(f"[{label.strip()}]" for label in re.split(r"[,;]", m.group(1))), answer_text)
            insufficient = bool(generation.data.get("insufficient_evidence"))
    except (SearchError, GenerationError) as exc:
        conn.execute("UPDATE graphrag.query_run SET status = 'FAILED', completed_at = now(), latency_ms = %s, "
                     "failure_detail = %s::jsonb WHERE id = %s", round((time.perf_counter() - started) * 1000),
                     json.dumps({"error": getattr(exc, "failure_code", type(exc).__name__), "message": str(exc)[:300]}),
                     str(query_run_id))
        raise

    by_label = {item.label: item for item in context}
    cited = list(dict.fromkeys(_LABEL.findall(answer_text)))
    valid = [label for label in cited if label in by_label]
    invalid = [label for label in cited if label not in by_label]
    if insufficient:
        grounding = "INSUFFICIENT_EVIDENCE"
    elif not valid:
        grounding = "UNSUPPORTED"
    elif invalid:
        grounding = "PARTIALLY_SUPPORTED"
    else:
        grounding = "SUPPORTED"
    answer_id = conn.fetch_scalar(
        "INSERT INTO graphrag.answer (query_run_id, answer_text, grounding_status, unsupported_claim_count) "
        "VALUES (%s, %s, %s, %s) RETURNING id", str(query_run_id), answer_text, grounding, len(invalid))
    citations = []
    for number, label in enumerate(valid, start=1):
        item = by_label[label]
        for evidence_id in item.evidence_ids[:3]:
            conn.execute("INSERT INTO graphrag.answer_citation (answer_id, claim_number, evidence_id, citation_status) "
                         "VALUES (%s, %s, %s, 'VALID') ON CONFLICT DO NOTHING", str(answer_id), number, evidence_id)
        citations.append({"label": label, "kind": item.kind, **item.display})
    latency = round((time.perf_counter() - started) * 1000)
    input_tokens = generation.input_tokens if generation else 0
    output_tokens = generation.output_tokens if generation else 0
    conn.execute(
        "UPDATE graphrag.query_run SET status = %s, completed_at = now(), latency_ms = %s, input_tokens = %s, "
        "output_tokens = %s, model_call_count = %s WHERE id = %s",
        "SUCCEEDED" if grounding in ("SUPPORTED", "INSUFFICIENT_EVIDENCE") else "DEGRADED", latency, input_tokens,
        output_tokens, 1 if generation else 0, str(query_run_id))
    return Answer(query_run_id, mode, answer_text, grounding, citations, context, invalid, latency, input_tokens,
                  output_tokens)


class _Trace:
    def __init__(self, conn: Any, query_run_id: UUID):
        self.conn, self.query_run_id, self.sequence = conn, query_run_id, 0

    def event(self, event_type: str, payload: dict, began: float | None = None) -> None:
        duration = round((time.perf_counter() - began) * 1000) if began is not None else None
        self.conn.execute("INSERT INTO graphrag.trace_event (query_run_id, sequence_number, event_type, duration_ms, "
                          "payload) VALUES (%s, %s, %s, %s, %s::jsonb)", str(self.query_run_id), self.sequence,
                          event_type, duration, json.dumps(payload))
        self.sequence += 1


def _local_context(conn: Any, analysis_id: UUID, question: str, graph: ProjectedGraph, run, trace: _Trace, *,
                   embedder: EmbeddingProvider, chunk_profile: ChunkProfile,
                   embedding_profile: EmbeddingProfile) -> list[ContextItem]:
    began = time.perf_counter()
    result = search_analysis(conn, analysis_id, question, embedder, chunk_profile, embedding_profile, k=SEEDS)
    trace.event("seed", {"hits": len(result.hits), "embed_ms": round(result.embed_ms),
                         "search_ms": round(result.search_ms)}, began)
    by_key = {node.stable_key: node.id for node in graph.nodes.values()}
    seeds = [by_key.get(hit.chunk.entity_stable_key) for hit in result.hits]
    seed_ids = [s for s in seeds if s is not None]

    began = time.perf_counter()
    scores: dict[UUID, float] = {}
    reasons: dict[UUID, str] = {}
    for rank, seed in enumerate(seed_ids):
        for neighbour, relation, outgoing in graph.neighbors(seed):
            node = graph.nodes[neighbour]
            if neighbour in seed_ids or node.entity_type == "FILE" or relation not in _EXPAND:
                continue
            if relation == "CONTAINS" and outgoing:
                continue  # only the container of a seed, not every member of a class
            scores[neighbour] = scores.get(neighbour, 0.0) + _EXPAND[relation] / (1 + rank)
            direction = "calls" if outgoing else "called by"
            reasons.setdefault(neighbour, f"{relation.lower()} ({direction if relation == 'CALLS' else 'related'}) "
                                          f"{graph.nodes[seed].name}")
    expanded = sorted(scores, key=lambda n: (-scores[n], graph.nodes[n].stable_key))[:EXPANSION]
    trace.event("expand", {"seeds": len(seed_ids), "candidates": len(scores), "expanded": len(expanded),
                           "max_hops": 1}, began)

    evidence = definition_evidence(conn, list(dict.fromkeys(seed_ids + expanded)))
    items = _hit_items(result.hits, by_key, evidence)
    excerpts = dict(conn.fetch_all(
        "SELECT DISTINCT ON (c.entity_id) c.entity_id, c.content FROM semantic.chunk c "
        "WHERE c.entity_id = ANY(%s::uuid[]) AND c.chunk_profile_id = (SELECT id FROM semantic.chunk_profile "
        "WHERE configuration_fingerprint = %s) ORDER BY c.entity_id, c.ordinal",
        [str(e) for e in expanded], chunk_profile.fingerprint)) if expanded else {}
    for node_id in expanded:
        node = graph.nodes[node_id]
        content = excerpts.get(node_id, "")
        items.append(ContextItem(
            f"E{len(items) + 1}", "code",
            _code_block(node.qualified_name or node.name, node.path, node.start_line, node.end_line, content),
            [str(evidence[node_id])] if node_id in evidence else [], "ENTITY", str(node_id), scores[node_id],
            f"graph: {reasons[node_id]}",
            _display(node.qualified_name or node.name, node.entity_type, node.path, node.start_line, node.end_line,
                     content)))
    if run is not None and seed_ids:
        items += _community_items(conn, run, seed_ids, start=1)
    return items


def _community_items(conn: Any, run, seed_ids: list[UUID], *, start: int) -> list[ContextItem]:
    seeds = {str(s) for s in seed_ids}
    rows = conn.fetch_all(
        "SELECT c.id, c.hierarchy_level, count(*) AS hits, s.structured_summary FROM graphrag.community c "
        "JOIN graphrag.community_member m ON m.community_id = c.id "
        "JOIN graphrag.community_summary s ON s.community_id = c.id AND s.status = 'READY' "
        "WHERE c.community_run_id = %s AND m.entity_id = ANY(%s::uuid[]) "
        "GROUP BY c.id, c.hierarchy_level, s.structured_summary ORDER BY count(*) DESC, c.hierarchy_level DESC",
        str(run.id), list(seeds))
    items = []
    for community_id, level, hits, summary in rows[:COMMUNITIES_LOCAL]:
        items.append(_summary_item(f"C{start + len(items)}", community_id, summary, float(hits),
                                   f"contains {hits} seed(s)"))
    return items


def _hit_items(hits, by_key: dict[str, UUID], evidence: dict[UUID, UUID]) -> list[ContextItem]:
    items: list[ContextItem] = []
    for hit in hits:
        node_id = by_key.get(hit.chunk.entity_stable_key)
        items.append(ContextItem(
            f"E{len(items) + 1}", "code", _code_block(hit.entity_name or hit.chunk.entity_stable_key, hit.chunk.file_path,
                                                     hit.chunk.start_line, hit.chunk.end_line, hit.content),
            [str(evidence[node_id])] if node_id in evidence else [], "CHUNK", str(hit.chunk.chunk_id),
            hit.chunk.score, "vector seed",
            _display(hit.entity_name, hit.entity_type, hit.chunk.file_path, hit.chunk.start_line, hit.chunk.end_line,
                     hit.content)))
    return items


def _global_anchors(conn: Any, analysis_id: UUID, question: str, graph: ProjectedGraph, trace: _Trace, *,
                    embedder: EmbeddingProvider, chunk_profile: ChunkProfile,
                    embedding_profile: EmbeddingProfile) -> list[ContextItem]:
    """A few vector hits beside the summaries: configuration and files outside the graph join no
    community, so without them a repository-wide question about deployment or settings sees nothing."""
    began = time.perf_counter()
    result = search_analysis(conn, analysis_id, question, embedder, chunk_profile, embedding_profile, k=GLOBAL_ANCHORS)
    trace.event("anchor", {"hits": len(result.hits)}, began)
    by_key = {node.stable_key: node.id for node in graph.nodes.values()}
    evidence = definition_evidence(conn, [n for n in (by_key.get(h.chunk.entity_stable_key) for h in result.hits) if n])
    return _hit_items(result.hits, by_key, evidence)


def _global_context(conn: Any, question: str, run, trace: _Trace) -> list[ContextItem]:
    began = time.perf_counter()
    if run is None:
        return []
    rows = conn.fetch_all(
        "SELECT c.id, c.hierarchy_level, c.member_count, s.summary_text, s.structured_summary "
        "FROM graphrag.community c JOIN graphrag.community_summary s ON s.community_id = c.id AND s.status = 'READY' "
        "WHERE c.community_run_id = %s", str(run.id))
    terms = _terms(question)
    scored = []
    for community_id, level, members, text, summary in rows:
        words = _terms(text or "")
        overlap = len(terms & words)
        score = overlap / math.sqrt(len(words) or 1) + 0.02 * math.log1p(members) + (0.05 if level == 0 else 0.0)
        scored.append((score, overlap, community_id, summary))
    scored.sort(key=lambda row: (-row[0], str(row[2])))
    if not any(overlap for _, overlap, _, _ in scored):  # nothing matches: give the top-level map
        scored = [row for row in scored if row[3].get("member_count", 0)][:COMMUNITIES_GLOBAL]
    chosen = scored[:COMMUNITIES_GLOBAL]
    trace.event("rank_communities", {"candidates": len(rows), "selected": len(chosen),
                                     "matched_terms": sorted(terms)[:20]}, began)
    return [_summary_item(f"C{i}", community_id, summary, score, f"{overlap} matching terms")
            for i, (score, overlap, community_id, summary) in enumerate(chosen, start=1)]


def _summary_item(label: str, community_id: UUID, summary: dict, score: float, reason: str) -> ContextItem:
    points = "\n".join(f"- {p['text']}" for p in summary.get("key_points", []) if p.get("supported"))
    files = sorted({ref.get("file") for ref in summary.get("references", {}).values() if ref.get("file")})
    text = f"Community summary: {summary.get('title')}\n{summary.get('summary')}\n{points}"
    if files:
        text += "\nFiles: " + ", ".join(files[:8])
    return ContextItem(label, "community", text, list(summary.get("evidence_ids", []))[:5], "COMMUNITY",
                       str(community_id), score, reason,
                       {"title": summary.get("title"), "summary": summary.get("summary"), "files": files[:8]})


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9]{2,}", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())
    return {w.rstrip("s") for w in words if w not in _STOP}


def _code_block(name: str | None, path: str | None, start: int | None, end: int | None, content: str) -> str:
    location = f"{path}:{start}-{end}" if start else (path or "")
    return f"{name or path} ({location})\n```\n{(content or '')[:EXCERPT_CHARS]}\n```"


def _display(name, entity_type, path, start, end, content) -> dict:
    return {"name": name, "entity_type": entity_type, "file_path": path, "start_line": start, "end_line": end,
            "snippet": (content or "")[:EXCERPT_CHARS]}


def _prompt(question: str, context: list[ContextItem]) -> str:
    parts = [f"[{item.label}] {item.text}" for item in context]
    return "Context:\n\n" + "\n\n".join(parts) + f"\n\nQuestion: {question}"


def _store_retrieval(conn: Any, query_run_id: UUID, context: list[ContextItem]) -> None:
    column = {"CHUNK": "chunk_id", "ENTITY": "entity_id", "COMMUNITY": "community_id"}
    for rank, item in enumerate(context, start=1):
        conn.execute(
            f"INSERT INTO graphrag.retrieval_item (query_run_id, rank, source_type, {column[item.source_type]}, "
            "vector_score, graph_score, combined_score, selection_reason, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            str(query_run_id), rank, item.source_type, item.source_id,
            item.score if item.source_type == "CHUNK" else None,
            item.score if item.source_type != "CHUNK" else None, item.score, item.reason,
            json.dumps({"label": item.label}))
