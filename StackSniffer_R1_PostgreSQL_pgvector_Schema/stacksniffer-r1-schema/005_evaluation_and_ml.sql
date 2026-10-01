BEGIN;

-- Frozen benchmark datasets, relevance judgments on canonical source facts, and evaluation
-- runs that record every material input version.
--
-- A dataset is DRAFT while it is assembled. Freezing stores its canonical manifest, which must
-- equal evaluation.dataset_manifest() built from the rows, and the manifest's SHA-256 as the
-- fingerprint. A frozen dataset and everything in it are immutable; a correction is a new version.

-- --- datasets -----------------------------------------------------------------------------------

CREATE TABLE evaluation.dataset (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_key TEXT NOT NULL,
    dataset_version INTEGER NOT NULL,
    description TEXT NOT NULL,
    code_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    canonical_manifest TEXT,
    manifest JSONB,
    fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    frozen_at TIMESTAMPTZ,
    CONSTRAINT evaluation_dataset_key_not_blank CHECK (btrim(dataset_key) <> ''),
    CONSTRAINT evaluation_dataset_version_ck CHECK (dataset_version > 0),
    CONSTRAINT evaluation_dataset_status_ck CHECK (status IN ('DRAFT', 'FROZEN')),
    CONSTRAINT evaluation_dataset_frozen_ck CHECK (
        (status = 'DRAFT' AND canonical_manifest IS NULL AND manifest IS NULL AND fingerprint IS NULL
         AND frozen_at IS NULL)
        OR (status = 'FROZEN' AND canonical_manifest IS NOT NULL AND manifest IS NOT NULL
            AND fingerprint IS NOT NULL AND frozen_at IS NOT NULL)
    ),
    CONSTRAINT evaluation_dataset_manifest_ck CHECK (manifest IS NULL OR manifest = canonical_manifest::jsonb),
    CONSTRAINT evaluation_dataset_fingerprint_ck CHECK (
        fingerprint IS NULL OR fingerprint = encode(sha256(convert_to(canonical_manifest, 'UTF8')), 'hex')
    ),
    CONSTRAINT evaluation_dataset_key_version_uq UNIQUE (dataset_key, dataset_version),
    CONSTRAINT evaluation_dataset_fingerprint_uq UNIQUE (fingerprint),
    -- Evaluation runs reference (id, fingerprint, FROZEN) so a run can only use a frozen dataset.
    CONSTRAINT evaluation_dataset_frozen_identity_uq UNIQUE (id, fingerprint, status)
);

CREATE TABLE evaluation.dataset_repository (
    dataset_id UUID NOT NULL REFERENCES evaluation.dataset(id) ON DELETE CASCADE,
    repository_version_id UUID NOT NULL REFERENCES core.repository_version(id) ON DELETE RESTRICT,
    dataset_role TEXT NOT NULL DEFAULT 'TEST',
    PRIMARY KEY (dataset_id, repository_version_id),
    CONSTRAINT dataset_repository_role_ck CHECK (dataset_role IN ('TRAIN', 'VALIDATION', 'TEST', 'DEMO'))
);

CREATE TABLE evaluation.question (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL REFERENCES evaluation.dataset(id) ON DELETE CASCADE,
    repository_version_id UUID NOT NULL,
    question_key TEXT NOT NULL,
    question_text TEXT NOT NULL,
    task_category TEXT NOT NULL,
    expected_answer_scope TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT evaluation_question_key_not_blank CHECK (btrim(question_key) <> ''),
    CONSTRAINT evaluation_question_not_blank CHECK (btrim(question_text) <> ''),
    CONSTRAINT evaluation_question_scope_ck CHECK (
        expected_answer_scope IN ('SYMBOL', 'FILE', 'MODULE', 'REPOSITORY')
    ),
    -- A question is about one of the dataset's repositories.
    CONSTRAINT evaluation_question_repository_fk
        FOREIGN KEY (dataset_id, repository_version_id)
        REFERENCES evaluation.dataset_repository (dataset_id, repository_version_id),
    CONSTRAINT evaluation_question_key_uq UNIQUE (dataset_id, question_key),
    CONSTRAINT evaluation_question_id_dataset_uq UNIQUE (id, dataset_id),
    CONSTRAINT evaluation_question_id_version_uq UNIQUE (id, repository_version_id)
);

-- Judgments name canonical source facts, never chunks, so a new chunking strategy is scored
-- against the same benchmark. A chunk is relevant when it contains or references the target.
CREATE TABLE evaluation.relevance_judgment (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id UUID NOT NULL,
    repository_version_id UUID NOT NULL,
    entity_stable_key TEXT,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    content_hash TEXT,
    relevance_grade INTEGER NOT NULL,
    judgment_source TEXT NOT NULL,
    annotator TEXT NOT NULL,
    annotator_version TEXT NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The target lives in the question's repository version.
    CONSTRAINT relevance_judgment_question_fk
        FOREIGN KEY (question_id, repository_version_id)
        REFERENCES evaluation.question (id, repository_version_id)
        ON DELETE CASCADE,
    CONSTRAINT relevance_judgment_target_ck CHECK (
        entity_stable_key IS NOT NULL OR (file_path IS NOT NULL AND start_line IS NOT NULL)
    ),
    CONSTRAINT relevance_judgment_lines_ck CHECK (
        (start_line IS NULL AND end_line IS NULL) OR (start_line > 0 AND end_line >= start_line)
    ),
    CONSTRAINT relevance_judgment_hash_ck CHECK (content_hash IS NULL OR start_line IS NOT NULL),
    CONSTRAINT relevance_judgment_grade_ck CHECK (relevance_grade BETWEEN 0 AND 3),
    CONSTRAINT relevance_judgment_source_ck CHECK (judgment_source IN ('HUMAN', 'MODEL_ASSISTED', 'DERIVED'))
);

CREATE UNIQUE INDEX relevance_judgment_target_uq
    ON evaluation.relevance_judgment (
        question_id, annotator, COALESCE(entity_stable_key, ''), COALESCE(file_path, ''),
        COALESCE(start_line, 0), COALESCE(end_line, 0)
    );
CREATE INDEX relevance_judgment_question_idx
    ON evaluation.relevance_judgment (question_id);

-- The manifest a frozen dataset's fingerprint covers, built from its rows in a fixed order.
CREATE OR REPLACE FUNCTION evaluation.dataset_manifest(requested_dataset UUID)
RETURNS JSONB
LANGUAGE sql
STABLE
SET search_path = evaluation, core, public, pg_temp
AS $$
SELECT jsonb_build_object(
    'dataset_key', d.dataset_key,
    'dataset_version', d.dataset_version,
    'description', d.description,
    'code_version', d.code_version,
    'repositories', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
                   'repository_key', r.canonical_key, 'commit_sha', v.commit_sha, 'role', dr.dataset_role)
               ORDER BY r.canonical_key, v.commit_sha)
          FROM evaluation.dataset_repository dr
          JOIN core.repository_version v ON v.id = dr.repository_version_id
          JOIN core.repository r ON r.id = v.repository_id
         WHERE dr.dataset_id = d.id), '[]'::jsonb),
    'questions', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
                   'question_key', q.question_key, 'repository_key', r.canonical_key, 'commit_sha', v.commit_sha,
                   'question_text', q.question_text, 'task_category', q.task_category,
                   'expected_answer_scope', q.expected_answer_scope, 'metadata', q.metadata,
                   'judgments', COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                                  'entity_stable_key', j.entity_stable_key, 'file_path', j.file_path,
                                  'start_line', j.start_line, 'end_line', j.end_line,
                                  'content_hash', j.content_hash, 'relevance_grade', j.relevance_grade,
                                  'judgment_source', j.judgment_source, 'annotator', j.annotator,
                                  'annotator_version', j.annotator_version, 'notes', j.notes)
                              ORDER BY j.annotator, COALESCE(j.entity_stable_key, ''), COALESCE(j.file_path, ''),
                                       COALESCE(j.start_line, 0), COALESCE(j.end_line, 0))
                         FROM evaluation.relevance_judgment j
                        WHERE j.question_id = q.id), '[]'::jsonb))
               ORDER BY q.question_key)
          FROM evaluation.question q
          JOIN core.repository_version v ON v.id = q.repository_version_id
          JOIN core.repository r ON r.id = v.repository_id
         WHERE q.dataset_id = d.id), '[]'::jsonb)
)
  FROM evaluation.dataset d
 WHERE d.id = requested_dataset;
$$;

-- --- frozen-dataset immutability ----------------------------------------------------------------

CREATE OR REPLACE FUNCTION evaluation.guard_dataset()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = evaluation, public, pg_temp
AS $$
BEGIN
    IF OLD.status = 'FROZEN' THEN
        RAISE EXCEPTION 'dataset %/% is frozen; publish a new version instead', OLD.dataset_key, OLD.dataset_version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.status = 'FROZEN' THEN
        IF (NEW.dataset_key, NEW.dataset_version, NEW.description, NEW.code_version)
           IS DISTINCT FROM (OLD.dataset_key, OLD.dataset_version, OLD.description, OLD.code_version) THEN
            RAISE EXCEPTION 'a dataset is frozen as it is; change it before freezing'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM evaluation.question WHERE dataset_id = NEW.id) THEN
            RAISE EXCEPTION 'dataset %/% has no questions', NEW.dataset_key, NEW.dataset_version
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.manifest IS DISTINCT FROM evaluation.dataset_manifest(NEW.id) THEN
            RAISE EXCEPTION 'the manifest does not describe dataset %/% as stored', NEW.dataset_key,
                NEW.dataset_version USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER dataset_guard_trg
BEFORE UPDATE OR DELETE ON evaluation.dataset
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_dataset();

-- Content of a dataset may change only while it is a draft. The share lock makes a concurrent
-- freeze wait for this change (and this change wait for a freeze in progress).
CREATE OR REPLACE FUNCTION evaluation.assert_draft(requested_dataset UUID)
RETURNS void
LANGUAGE plpgsql
SET search_path = evaluation, public, pg_temp
AS $$
DECLARE
    current_status TEXT;
BEGIN
    SELECT status INTO current_status FROM evaluation.dataset WHERE id = requested_dataset FOR SHARE;
    IF current_status = 'FROZEN' THEN
        RAISE EXCEPTION 'dataset % is frozen; its repositories, questions and judgments cannot change',
            requested_dataset USING ERRCODE = 'integrity_constraint_violation';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION evaluation.guard_dataset_content()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = evaluation, public, pg_temp
AS $$
BEGIN
    IF TG_TABLE_NAME = 'relevance_judgment' THEN
        IF TG_OP <> 'INSERT' THEN
            PERFORM evaluation.assert_draft((SELECT dataset_id FROM evaluation.question WHERE id = OLD.question_id));
        END IF;
        IF TG_OP <> 'DELETE' THEN
            PERFORM evaluation.assert_draft((SELECT dataset_id FROM evaluation.question WHERE id = NEW.question_id));
        END IF;
    ELSE
        IF TG_OP <> 'INSERT' THEN
            PERFORM evaluation.assert_draft(OLD.dataset_id);
        END IF;
        IF TG_OP <> 'DELETE' THEN
            PERFORM evaluation.assert_draft(NEW.dataset_id);
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER dataset_repository_guard_trg
BEFORE INSERT OR UPDATE OR DELETE ON evaluation.dataset_repository
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_dataset_content();
CREATE TRIGGER question_guard_trg
BEFORE INSERT OR UPDATE OR DELETE ON evaluation.question
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_dataset_content();
CREATE TRIGGER relevance_judgment_guard_trg
BEFORE INSERT OR UPDATE OR DELETE ON evaluation.relevance_judgment
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_dataset_content();

-- --- evaluation runs ----------------------------------------------------------------------------

CREATE TABLE evaluation.run (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id UUID NOT NULL,
    dataset_fingerprint TEXT NOT NULL,
    dataset_status TEXT NOT NULL DEFAULT 'FROZEN',
    chunk_profile_id UUID NOT NULL,
    chunk_profile_fingerprint TEXT NOT NULL,
    embedding_profile_id UUID NOT NULL,
    embedding_profile_fingerprint TEXT NOT NULL,
    retrieval_mode TEXT NOT NULL,
    retriever_profile TEXT NOT NULL,
    retriever_version TEXT NOT NULL,
    graph_expansion_profile TEXT,
    graph_expansion_version TEXT,
    graph_snapshot_id UUID REFERENCES graphrag.graph_snapshot(id) ON DELETE SET NULL,
    community_run_id UUID REFERENCES graphrag.community_run(id) ON DELETE SET NULL,
    query_transformation_version TEXT NOT NULL,
    evaluation_code_version TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    metric_configuration JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    failure_detail JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT evaluation_run_dataset_frozen_ck CHECK (dataset_status = 'FROZEN'),
    CONSTRAINT evaluation_run_dataset_fk
        FOREIGN KEY (dataset_id, dataset_fingerprint, dataset_status)
        REFERENCES evaluation.dataset (id, fingerprint, status),
    CONSTRAINT evaluation_run_chunk_profile_fk
        FOREIGN KEY (chunk_profile_id, chunk_profile_fingerprint)
        REFERENCES semantic.chunk_profile (id, configuration_fingerprint),
    CONSTRAINT evaluation_run_embedding_profile_fk
        FOREIGN KEY (embedding_profile_id, embedding_profile_fingerprint)
        REFERENCES semantic.embedding_profile (id, configuration_fingerprint),
    CONSTRAINT evaluation_run_mode_ck CHECK (
        retrieval_mode IN ('VECTOR_ONLY', 'GRAPH_EXPANDED', 'LOCAL_GRAPHRAG', 'GLOBAL_GRAPHRAG')
    ),
    -- Graph-based modes record their expansion profile; vector-only runs have none.
    CONSTRAINT evaluation_run_graph_expansion_ck CHECK (
        (graph_expansion_profile IS NULL) = (graph_expansion_version IS NULL)
        AND (retrieval_mode = 'VECTOR_ONLY') = (graph_expansion_profile IS NULL)
    ),
    CONSTRAINT evaluation_run_versions_not_blank CHECK (
        btrim(retriever_version) <> '' AND btrim(query_transformation_version) <> ''
        AND btrim(evaluation_code_version) <> '' AND btrim(git_commit) <> ''
    ),
    CONSTRAINT evaluation_run_status_ck CHECK (status IN ('RUNNING', 'SUCCEEDED', 'DEGRADED', 'FAILED')),
    CONSTRAINT evaluation_run_completion_ck CHECK ((status = 'RUNNING') = (completed_at IS NULL)),
    CONSTRAINT evaluation_run_failure_ck CHECK (status <> 'FAILED' OR failure_detail IS NOT NULL),
    CONSTRAINT evaluation_run_id_dataset_uq UNIQUE (id, dataset_id)
);

CREATE INDEX evaluation_run_dataset_profiles_idx
    ON evaluation.run (dataset_id, chunk_profile_id, embedding_profile_id, started_at);

-- The analysis each of the dataset's repository versions was evaluated against.
CREATE TABLE evaluation.run_analysis (
    evaluation_run_id UUID NOT NULL,
    dataset_id UUID NOT NULL,
    repository_version_id UUID NOT NULL,
    analysis_id UUID NOT NULL,
    PRIMARY KEY (evaluation_run_id, repository_version_id),
    CONSTRAINT run_analysis_run_fk
        FOREIGN KEY (evaluation_run_id, dataset_id) REFERENCES evaluation.run (id, dataset_id) ON DELETE CASCADE,
    CONSTRAINT run_analysis_repository_fk
        FOREIGN KEY (dataset_id, repository_version_id)
        REFERENCES evaluation.dataset_repository (dataset_id, repository_version_id),
    CONSTRAINT run_analysis_analysis_fk
        FOREIGN KEY (analysis_id, repository_version_id)
        REFERENCES core.analysis (id, repository_version_id) ON DELETE RESTRICT
);

CREATE TABLE evaluation.question_result (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    evaluation_run_id UUID NOT NULL,
    dataset_id UUID NOT NULL,
    question_id UUID NOT NULL,
    query_run_id UUID REFERENCES graphrag.query_run(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    latency_ms DOUBLE PRECISION,
    failure_detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT question_result_run_fk
        FOREIGN KEY (evaluation_run_id, dataset_id) REFERENCES evaluation.run (id, dataset_id) ON DELETE CASCADE,
    -- Only questions of the run's dataset.
    CONSTRAINT question_result_question_fk
        FOREIGN KEY (question_id, dataset_id) REFERENCES evaluation.question (id, dataset_id),
    CONSTRAINT question_result_status_ck CHECK (status IN ('SUCCEEDED', 'DEGRADED', 'FAILED', 'SKIPPED')),
    CONSTRAINT question_result_failure_ck CHECK (status <> 'FAILED' OR failure_detail IS NOT NULL),
    CONSTRAINT question_result_counts_ck CHECK (result_count >= 0 AND (latency_ms IS NULL OR latency_ms >= 0)),
    CONSTRAINT question_result_uq UNIQUE (evaluation_run_id, question_id)
);

-- Per-query rankings, kept so every metric can be audited. The chunk's identity is copied so the
-- ranking survives even if the chunk's analysis is later deleted.
CREATE TABLE evaluation.ranked_result (
    evaluation_run_id UUID NOT NULL,
    question_id UUID NOT NULL,
    rank INTEGER NOT NULL,
    chunk_id UUID REFERENCES semantic.chunk(id) ON DELETE SET NULL,
    stable_chunk_key TEXT NOT NULL,
    chunk_content_hash TEXT NOT NULL,
    file_path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    entity_stable_key TEXT,
    score DOUBLE PRECISION NOT NULL,
    vector_score DOUBLE PRECISION,
    graph_score DOUBLE PRECISION,
    lexical_score DOUBLE PRECISION,
    relevance_grade INTEGER NOT NULL,
    is_relevant BOOLEAN NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (evaluation_run_id, question_id, rank),
    CONSTRAINT ranked_result_question_fk
        FOREIGN KEY (evaluation_run_id, question_id)
        REFERENCES evaluation.question_result (evaluation_run_id, question_id) ON DELETE CASCADE,
    CONSTRAINT ranked_result_rank_ck CHECK (rank > 0),
    CONSTRAINT ranked_result_grade_ck CHECK (relevance_grade BETWEEN 0 AND 3 AND is_relevant = (relevance_grade > 0))
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

-- --- completed runs are immutable ---------------------------------------------------------------

CREATE OR REPLACE FUNCTION evaluation.guard_run()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = evaluation, public, pg_temp
AS $$
BEGIN
    IF OLD.status <> 'RUNNING' THEN
        RAISE EXCEPTION 'evaluation run % is complete and immutable', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - 'status' - 'completed_at' - 'failure_detail')
                            IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'completed_at' - 'failure_detail') THEN
        RAISE EXCEPTION 'an evaluation run''s inputs cannot change' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER run_guard_trg
BEFORE UPDATE OR DELETE ON evaluation.run
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_run();

CREATE OR REPLACE FUNCTION evaluation.guard_run_results()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = evaluation, public, pg_temp
AS $$
DECLARE
    run_id UUID := CASE WHEN TG_OP = 'DELETE' THEN OLD.evaluation_run_id ELSE NEW.evaluation_run_id END;
    current_status TEXT;
BEGIN
    SELECT status INTO current_status FROM evaluation.run WHERE id = run_id FOR SHARE;
    -- A ranked chunk may be deleted with its analysis's derived data: the ranking keeps its copied
    -- identity and only loses the link (ON DELETE SET NULL). Nothing else may change.
    -- (Nested and read through jsonb: the other guarded tables have no chunk_id column.)
    IF TG_TABLE_NAME = 'ranked_result' AND TG_OP = 'UPDATE' THEN
        IF to_jsonb(NEW) ->> 'chunk_id' IS NULL
           AND (to_jsonb(NEW) - 'chunk_id') = (to_jsonb(OLD) - 'chunk_id') THEN
            RETURN NEW;
        END IF;
    END IF;
    IF current_status IS NOT NULL AND current_status <> 'RUNNING' THEN
        RAISE EXCEPTION 'evaluation run % is complete; its results cannot change', run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER run_analysis_guard_trg
BEFORE INSERT OR UPDATE OR DELETE ON evaluation.run_analysis
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_run_results();
CREATE TRIGGER question_result_guard_trg
BEFORE INSERT OR UPDATE OR DELETE ON evaluation.question_result
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_run_results();
CREATE TRIGGER ranked_result_guard_trg
BEFORE INSERT OR UPDATE OR DELETE ON evaluation.ranked_result
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_run_results();
CREATE TRIGGER metric_guard_trg
BEFORE INSERT OR UPDATE OR DELETE ON evaluation.metric
FOR EACH ROW EXECUTE FUNCTION evaluation.guard_run_results();

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
