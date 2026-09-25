BEGIN;

CREATE TABLE graphrag.graph_snapshot (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE CASCADE,
    graph_schema_version TEXT NOT NULL,
    projection_version TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'BUILDING',
    node_count BIGINT,
    edge_count BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    failure_detail JSONB,
    CONSTRAINT graph_snapshot_status_ck CHECK (
        status IN ('BUILDING', 'VALIDATING', 'ACTIVE', 'STALE', 'FAILED', 'RETIRED')
    ),
    CONSTRAINT graph_snapshot_counts_ck CHECK (
        (node_count IS NULL OR node_count >= 0)
        AND (edge_count IS NULL OR edge_count >= 0)
    ),
    CONSTRAINT graph_snapshot_identity_uq UNIQUE (
        analysis_id, graph_schema_version, projection_version, content_fingerprint
    )
);

CREATE UNIQUE INDEX graph_snapshot_one_active_per_analysis_uq
    ON graphrag.graph_snapshot (analysis_id)
    WHERE status = 'ACTIVE';

CREATE TABLE graphrag.projection_checkpoint (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_snapshot_id UUID NOT NULL REFERENCES graphrag.graph_snapshot(id) ON DELETE CASCADE,
    batch_type TEXT NOT NULL,
    batch_number INTEGER NOT NULL,
    last_canonical_id UUID,
    processed_count BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT projection_checkpoint_batch_number_ck CHECK (batch_number >= 0),
    CONSTRAINT projection_checkpoint_processed_ck CHECK (processed_count >= 0),
    CONSTRAINT projection_checkpoint_status_ck CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')
    ),
    CONSTRAINT projection_checkpoint_uq UNIQUE (graph_snapshot_id, batch_type, batch_number)
);

CREATE TABLE graphrag.community_run (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_snapshot_id UUID NOT NULL REFERENCES graphrag.graph_snapshot(id) ON DELETE CASCADE,
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    random_seed BIGINT,
    dataset_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    failure_detail JSONB,
    CONSTRAINT community_run_status_ck CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')
    ),
    CONSTRAINT community_run_identity_uq UNIQUE (
        graph_snapshot_id, algorithm, algorithm_version, dataset_fingerprint
    )
);

CREATE TABLE graphrag.community (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    community_run_id UUID NOT NULL REFERENCES graphrag.community_run(id) ON DELETE CASCADE,
    parent_community_id UUID REFERENCES graphrag.community(id) ON DELETE CASCADE,
    community_key TEXT NOT NULL,
    hierarchy_level INTEGER NOT NULL DEFAULT 0,
    member_count INTEGER NOT NULL DEFAULT 0,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT community_level_ck CHECK (hierarchy_level >= 0),
    CONSTRAINT community_member_count_ck CHECK (member_count >= 0),
    CONSTRAINT community_key_uq UNIQUE (community_run_id, community_key)
);

CREATE INDEX community_parent_idx
    ON graphrag.community (parent_community_id);

CREATE TABLE graphrag.community_member (
    community_id UUID NOT NULL REFERENCES graphrag.community(id) ON DELETE CASCADE,
    entity_id UUID NOT NULL REFERENCES core.entity(id) ON DELETE CASCADE,
    membership_score DOUBLE PRECISION,
    CONSTRAINT community_member_score_ck CHECK (
        membership_score IS NULL OR (membership_score >= 0 AND membership_score <= 1)
    ),
    PRIMARY KEY (community_id, entity_id)
);

CREATE INDEX community_member_entity_idx
    ON graphrag.community_member (entity_id);

CREATE TABLE graphrag.community_summary (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id UUID NOT NULL REFERENCES graphrag.community(id) ON DELETE CASCADE,
    summary_schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model_profile TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    summary_text TEXT,
    structured_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'GENERATING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT community_summary_not_blank CHECK (
        summary_text IS NULL OR btrim(summary_text) <> ''
    ),
    CONSTRAINT community_summary_status_ck CHECK (
        status IN ('GENERATING', 'READY', 'UNSUPPORTED', 'FAILED', 'STALE')
    ),
    CONSTRAINT community_summary_ready_text_ck CHECK (
        status <> 'READY' OR summary_text IS NOT NULL
    ),
    CONSTRAINT community_summary_identity_uq UNIQUE (
        community_id, summary_schema_version, prompt_version, model_profile, input_fingerprint
    )
);

CREATE TABLE graphrag.community_summary_citation (
    summary_id UUID NOT NULL REFERENCES graphrag.community_summary(id) ON DELETE CASCADE,
    evidence_id UUID NOT NULL REFERENCES core.evidence(id) ON DELETE CASCADE,
    claim_key TEXT NOT NULL,
    PRIMARY KEY (summary_id, evidence_id, claim_key)
);

CREATE TABLE graphrag.query_run (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE CASCADE,
    graph_snapshot_id UUID REFERENCES graphrag.graph_snapshot(id) ON DELETE SET NULL,
    community_run_id UUID REFERENCES graphrag.community_run(id) ON DELETE SET NULL,
    embedding_profile_id UUID NOT NULL REFERENCES semantic.embedding_profile(id) ON DELETE RESTRICT,
    query_text TEXT NOT NULL,
    query_type TEXT NOT NULL,
    retrieval_mode TEXT NOT NULL,
    retriever_version TEXT NOT NULL,
    prompt_version TEXT,
    model_profile TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    model_call_count INTEGER NOT NULL DEFAULT 0,
    failure_detail JSONB,
    CONSTRAINT query_run_text_not_blank CHECK (btrim(query_text) <> ''),
    CONSTRAINT query_run_type_ck CHECK (query_type IN ('LOCAL', 'GLOBAL')),
    CONSTRAINT query_run_mode_ck CHECK (
        retrieval_mode IN ('VECTOR_ONLY', 'GRAPH_EXPANDED', 'LOCAL_GRAPHRAG', 'GLOBAL_GRAPHRAG')
    ),
    CONSTRAINT query_run_status_ck CHECK (
        status IN ('RUNNING', 'SUCCEEDED', 'DEGRADED', 'FAILED', 'CANCELLED')
    ),
    CONSTRAINT query_run_nonnegative_metrics_ck CHECK (
        (latency_ms IS NULL OR latency_ms >= 0)
        AND (input_tokens IS NULL OR input_tokens >= 0)
        AND (output_tokens IS NULL OR output_tokens >= 0)
        AND model_call_count >= 0
    )
);

CREATE INDEX query_run_analysis_started_idx
    ON graphrag.query_run (analysis_id, started_at DESC);
CREATE INDEX query_run_mode_status_idx
    ON graphrag.query_run (retrieval_mode, status);

CREATE TABLE graphrag.trace_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_run_id UUID NOT NULL REFERENCES graphrag.query_run(id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    duration_ms INTEGER,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT trace_event_sequence_ck CHECK (sequence_number >= 0),
    CONSTRAINT trace_event_duration_ck CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT trace_event_sequence_uq UNIQUE (query_run_id, sequence_number)
);

CREATE INDEX trace_event_run_type_idx
    ON graphrag.trace_event (query_run_id, event_type);

CREATE TABLE graphrag.retrieval_item (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_run_id UUID NOT NULL REFERENCES graphrag.query_run(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    chunk_id UUID REFERENCES semantic.chunk(id) ON DELETE SET NULL,
    entity_id UUID REFERENCES core.entity(id) ON DELETE SET NULL,
    community_id UUID REFERENCES graphrag.community(id) ON DELETE SET NULL,
    vector_score DOUBLE PRECISION,
    graph_score DOUBLE PRECISION,
    combined_score DOUBLE PRECISION NOT NULL,
    selection_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT retrieval_item_rank_ck CHECK (rank > 0),
    CONSTRAINT retrieval_item_source_type_ck CHECK (
        source_type IN ('CHUNK', 'ENTITY', 'COMMUNITY')
    ),
    CONSTRAINT retrieval_item_one_source_ck CHECK (
        num_nonnulls(chunk_id, entity_id, community_id) = 1
    ),
    CONSTRAINT retrieval_item_rank_uq UNIQUE (query_run_id, rank)
);

CREATE TABLE graphrag.answer (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_run_id UUID NOT NULL UNIQUE REFERENCES graphrag.query_run(id) ON DELETE CASCADE,
    answer_text TEXT NOT NULL,
    grounding_status TEXT NOT NULL,
    unsupported_claim_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT answer_text_not_blank CHECK (btrim(answer_text) <> ''),
    CONSTRAINT answer_grounding_status_ck CHECK (
        grounding_status IN ('SUPPORTED', 'PARTIALLY_SUPPORTED', 'UNSUPPORTED', 'INSUFFICIENT_EVIDENCE')
    ),
    CONSTRAINT answer_unsupported_claims_ck CHECK (unsupported_claim_count >= 0)
);

CREATE TABLE graphrag.answer_citation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    answer_id UUID NOT NULL REFERENCES graphrag.answer(id) ON DELETE CASCADE,
    claim_number INTEGER NOT NULL,
    evidence_id UUID NOT NULL REFERENCES core.evidence(id) ON DELETE RESTRICT,
    citation_status TEXT NOT NULL DEFAULT 'PENDING',
    CONSTRAINT answer_citation_claim_ck CHECK (claim_number > 0),
    CONSTRAINT answer_citation_status_ck CHECK (
        citation_status IN ('PENDING', 'VALID', 'INVALID', 'PARTIAL')
    ),
    CONSTRAINT answer_citation_uq UNIQUE (answer_id, claim_number, evidence_id)
);

COMMIT;
