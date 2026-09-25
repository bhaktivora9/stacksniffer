BEGIN;

CREATE TABLE evaluation.dataset (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    rubric_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    frozen_at TIMESTAMPTZ,
    CONSTRAINT evaluation_dataset_status_ck CHECK (status IN ('DRAFT', 'FROZEN', 'RETIRED')),
    CONSTRAINT evaluation_dataset_name_version_uq UNIQUE (name, version)
);

CREATE TABLE evaluation.dataset_repository (
    dataset_id UUID NOT NULL REFERENCES evaluation.dataset(id) ON DELETE CASCADE,
    repository_version_id UUID NOT NULL REFERENCES core.repository_version(id) ON DELETE RESTRICT,
    dataset_role TEXT NOT NULL,
    PRIMARY KEY (dataset_id, repository_version_id),
    CONSTRAINT dataset_repository_role_ck CHECK (
        dataset_role IN ('TRAIN', 'VALIDATION', 'TEST', 'DEMO')
    )
);

CREATE TABLE evaluation.question (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL REFERENCES evaluation.dataset(id) ON DELETE CASCADE,
    repository_version_id UUID NOT NULL REFERENCES core.repository_version(id) ON DELETE RESTRICT,
    question_key TEXT NOT NULL,
    question_text TEXT NOT NULL,
    question_type TEXT NOT NULL,
    expected_capability TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT evaluation_question_not_blank CHECK (btrim(question_text) <> ''),
    CONSTRAINT evaluation_question_type_ck CHECK (question_type IN ('LOCAL', 'GLOBAL')),
    CONSTRAINT evaluation_question_key_uq UNIQUE (dataset_id, question_key)
);

CREATE TABLE evaluation.relevance_judgment (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id UUID NOT NULL REFERENCES evaluation.question(id) ON DELETE CASCADE,
    entity_id UUID REFERENCES core.entity(id) ON DELETE CASCADE,
    evidence_id UUID REFERENCES core.evidence(id) ON DELETE CASCADE,
    relevance_grade INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    rationale TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT relevance_judgment_target_ck CHECK (num_nonnulls(entity_id, evidence_id) = 1),
    CONSTRAINT relevance_judgment_grade_ck CHECK (relevance_grade BETWEEN 0 AND 3)
);

CREATE UNIQUE INDEX relevance_judgment_entity_uq
    ON evaluation.relevance_judgment (question_id, entity_id, reviewer)
    WHERE entity_id IS NOT NULL;
CREATE UNIQUE INDEX relevance_judgment_evidence_uq
    ON evaluation.relevance_judgment (question_id, evidence_id, reviewer)
    WHERE evidence_id IS NOT NULL;

CREATE TABLE evaluation.run (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL REFERENCES evaluation.dataset(id) ON DELETE RESTRICT,
    embedding_profile_id UUID NOT NULL REFERENCES semantic.embedding_profile(id) ON DELETE RESTRICT,
    graph_snapshot_id UUID REFERENCES graphrag.graph_snapshot(id) ON DELETE SET NULL,
    community_run_id UUID REFERENCES graphrag.community_run(id) ON DELETE SET NULL,
    retrieval_mode TEXT NOT NULL,
    retriever_version TEXT NOT NULL,
    prompt_version TEXT,
    model_profile TEXT,
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT evaluation_run_mode_ck CHECK (
        retrieval_mode IN ('VECTOR_ONLY', 'GRAPH_EXPANDED', 'LOCAL_GRAPHRAG', 'GLOBAL_GRAPHRAG')
    ),
    CONSTRAINT evaluation_run_status_ck CHECK (
        status IN ('RUNNING', 'SUCCEEDED', 'DEGRADED', 'FAILED')
    )
);

CREATE TABLE evaluation.question_result (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    evaluation_run_id UUID NOT NULL REFERENCES evaluation.run(id) ON DELETE CASCADE,
    question_id UUID NOT NULL REFERENCES evaluation.question(id) ON DELETE CASCADE,
    query_run_id UUID REFERENCES graphrag.query_run(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    failure_detail JSONB,
    CONSTRAINT question_result_status_ck CHECK (
        status IN ('SUCCEEDED', 'DEGRADED', 'FAILED', 'SKIPPED')
    ),
    CONSTRAINT question_result_uq UNIQUE (evaluation_run_id, question_id)
);

CREATE TABLE evaluation.metric (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    evaluation_run_id UUID NOT NULL REFERENCES evaluation.run(id) ON DELETE CASCADE,
    question_id UUID REFERENCES evaluation.question(id) ON DELETE CASCADE,
    metric_name TEXT NOT NULL,
    metric_value DOUBLE PRECISION NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX evaluation_metric_run_name_idx
    ON evaluation.metric (evaluation_run_id, metric_name);

CREATE UNIQUE INDEX evaluation_metric_aggregate_uq
    ON evaluation.metric (evaluation_run_id, metric_name)
    WHERE question_id IS NULL;
CREATE UNIQUE INDEX evaluation_metric_question_uq
    ON evaluation.metric (evaluation_run_id, question_id, metric_name)
    WHERE question_id IS NOT NULL;

CREATE TABLE ml.pair_snapshot (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    pair_rule_version TEXT NOT NULL,
    dataset_fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    frozen_at TIMESTAMPTZ,
    CONSTRAINT pair_snapshot_status_ck CHECK (status IN ('DRAFT', 'FROZEN', 'RETIRED')),
    CONSTRAINT pair_snapshot_name_version_uq UNIQUE (name, version)
);

CREATE TABLE ml.pair_snapshot_analysis (
    pair_snapshot_id UUID NOT NULL REFERENCES ml.pair_snapshot(id) ON DELETE CASCADE,
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE RESTRICT,
    split TEXT NOT NULL,
    PRIMARY KEY (pair_snapshot_id, analysis_id),
    CONSTRAINT pair_snapshot_analysis_split_ck CHECK (split IN ('TRAIN', 'VALIDATION', 'TEST'))
);

CREATE TABLE ml.contrastive_pair (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pair_snapshot_id UUID NOT NULL REFERENCES ml.pair_snapshot(id) ON DELETE CASCADE,
    anchor_chunk_id UUID NOT NULL REFERENCES semantic.chunk(id) ON DELETE RESTRICT,
    positive_chunk_id UUID NOT NULL REFERENCES semantic.chunk(id) ON DELETE RESTRICT,
    negative_chunk_id UUID REFERENCES semantic.chunk(id) ON DELETE RESTRICT,
    relationship_type TEXT NOT NULL,
    mining_rule TEXT NOT NULL,
    quality_status TEXT NOT NULL DEFAULT 'PENDING',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT contrastive_pair_anchor_positive_ck CHECK (anchor_chunk_id <> positive_chunk_id),
    CONSTRAINT contrastive_pair_negative_ck CHECK (
        negative_chunk_id IS NULL
        OR (negative_chunk_id <> anchor_chunk_id AND negative_chunk_id <> positive_chunk_id)
    ),
    CONSTRAINT contrastive_pair_quality_ck CHECK (
        quality_status IN ('PENDING', 'APPROVED', 'REJECTED', 'SUSPECT')
    )
);

CREATE INDEX contrastive_pair_snapshot_quality_idx
    ON ml.contrastive_pair (pair_snapshot_id, quality_status);
CREATE UNIQUE INDEX contrastive_pair_with_negative_uq
    ON ml.contrastive_pair (
        pair_snapshot_id, anchor_chunk_id, positive_chunk_id, negative_chunk_id, mining_rule
    )
    WHERE negative_chunk_id IS NOT NULL;
CREATE UNIQUE INDEX contrastive_pair_without_negative_uq
    ON ml.contrastive_pair (
        pair_snapshot_id, anchor_chunk_id, positive_chunk_id, mining_rule
    )
    WHERE negative_chunk_id IS NULL;

CREATE TABLE ml.training_run (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pair_snapshot_id UUID NOT NULL REFERENCES ml.pair_snapshot(id) ON DELETE RESTRICT,
    base_profile_id UUID NOT NULL REFERENCES semantic.embedding_profile(id) ON DELETE RESTRICT,
    candidate_profile_id UUID REFERENCES semantic.embedding_profile(id) ON DELETE RESTRICT,
    objective TEXT NOT NULL,
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    random_seed BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    artifact_uri TEXT,
    result_summary JSONB,
    failure_detail JSONB,
    CONSTRAINT training_run_status_ck CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'REJECTED', 'PROMOTED')
    ),
    CONSTRAINT training_run_profiles_ck CHECK (
        candidate_profile_id IS NULL OR candidate_profile_id <> base_profile_id
    )
);

COMMIT;
