BEGIN;

CREATE TABLE core.repository (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    clone_url TEXT NOT NULL,
    default_branch TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT repository_canonical_key_not_blank CHECK (btrim(canonical_key) <> ''),
    CONSTRAINT repository_clone_url_not_blank CHECK (btrim(clone_url) <> '')
);

CREATE UNIQUE INDEX repository_provider_owner_name_uq
    ON core.repository (lower(provider), lower(owner_name), lower(repository_name));

CREATE TABLE core.repository_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_id UUID NOT NULL REFERENCES core.repository(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    requested_ref TEXT,
    manifest_version TEXT,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT repository_version_commit_not_blank CHECK (btrim(commit_sha) <> ''),
    CONSTRAINT repository_version_repo_commit_uq UNIQUE (repository_id, commit_sha)
);

CREATE INDEX repository_version_repository_idx
    ON core.repository_version (repository_id, resolved_at DESC);

CREATE TABLE core.analysis (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_version_id UUID NOT NULL REFERENCES core.repository_version(id) ON DELETE CASCADE,
    structural_pipeline_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT analysis_pipeline_version_not_blank CHECK (btrim(structural_pipeline_version) <> ''),
    CONSTRAINT analysis_status_ck CHECK (
        status IN (
            'QUEUED', 'INGESTING', 'EXTRACTING', 'INDEXING', 'PROJECTING',
            'SUMMARIZING', 'READY', 'DEGRADED', 'FAILED'
        )
    ),
    CONSTRAINT analysis_identity_uq UNIQUE (repository_version_id, structural_pipeline_version)
);

CREATE INDEX analysis_status_created_idx
    ON core.analysis (status, created_at);

CREATE TABLE core.analysis_attempt (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    failure_stage TEXT,
    failure_code TEXT,
    failure_detail JSONB,
    CONSTRAINT analysis_attempt_number_positive CHECK (attempt_number > 0),
    CONSTRAINT analysis_attempt_status_ck CHECK (
        status IN ('RUNNING', 'SUCCEEDED', 'DEGRADED', 'FAILED', 'CANCELLED')
    ),
    CONSTRAINT analysis_attempt_uq UNIQUE (analysis_id, attempt_number)
);

CREATE TABLE core.source_file (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    language TEXT,
    content_hash TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    is_generated BOOLEAN NOT NULL DEFAULT false,
    is_vendored BOOLEAN NOT NULL DEFAULT false,
    parse_status TEXT NOT NULL DEFAULT 'PENDING',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT source_file_path_not_blank CHECK (btrim(path) <> ''),
    CONSTRAINT source_file_size_nonnegative CHECK (size_bytes >= 0),
    CONSTRAINT source_file_parse_status_ck CHECK (
        parse_status IN ('PENDING', 'PARSED', 'PARTIAL', 'UNSUPPORTED', 'FAILED', 'SKIPPED')
    ),
    CONSTRAINT source_file_analysis_path_uq UNIQUE (analysis_id, path),
    CONSTRAINT source_file_id_analysis_uq UNIQUE (id, analysis_id)
);

CREATE INDEX source_file_analysis_language_idx
    ON core.source_file (analysis_id, language);

CREATE TABLE core.entity (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE CASCADE,
    file_id UUID,
    parent_entity_id UUID,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT,
    stable_key TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    extractor_version TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT entity_type_ck CHECK (
        entity_type IN (
            'REPOSITORY', 'MODULE', 'PACKAGE', 'FILE', 'CLASS', 'INTERFACE',
            'METHOD', 'FUNCTION', 'ENDPOINT', 'DEPENDENCY', 'DATASTORE',
            'MESSAGE_TOPIC', 'TECHNOLOGY', 'CONFIGURATION', 'EXTERNAL_SYMBOL'
        )
    ),
    CONSTRAINT entity_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT entity_stable_key_not_blank CHECK (btrim(stable_key) <> ''),
    CONSTRAINT entity_line_range_ck CHECK (
        (start_line IS NULL AND end_line IS NULL)
        OR (start_line > 0 AND end_line >= start_line)
    ),
    CONSTRAINT entity_analysis_stable_key_uq UNIQUE (analysis_id, stable_key),
    CONSTRAINT entity_id_analysis_uq UNIQUE (id, analysis_id),
    CONSTRAINT entity_file_analysis_fk
        FOREIGN KEY (file_id, analysis_id)
        REFERENCES core.source_file (id, analysis_id)
        ON DELETE CASCADE,
    CONSTRAINT entity_parent_analysis_fk
        FOREIGN KEY (parent_entity_id, analysis_id)
        REFERENCES core.entity (id, analysis_id)
        ON DELETE CASCADE
);

CREATE INDEX entity_analysis_type_idx
    ON core.entity (analysis_id, entity_type);
CREATE INDEX entity_analysis_qualified_name_idx
    ON core.entity (analysis_id, qualified_name);
CREATE INDEX entity_parent_idx
    ON core.entity (parent_entity_id);

CREATE TABLE core.evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE CASCADE,
    file_id UUID NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_byte INTEGER,
    end_byte INTEGER,
    content_hash TEXT NOT NULL,
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    certainty TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT evidence_line_range_ck CHECK (start_line > 0 AND end_line >= start_line),
    CONSTRAINT evidence_byte_range_ck CHECK (
        (start_byte IS NULL AND end_byte IS NULL)
        OR (start_byte >= 0 AND end_byte >= start_byte)
    ),
    CONSTRAINT evidence_certainty_ck CHECK (certainty IN ('EXACT', 'HIGH', 'MEDIUM', 'LOW')),
    CONSTRAINT evidence_file_analysis_fk
        FOREIGN KEY (file_id, analysis_id)
        REFERENCES core.source_file (id, analysis_id)
        ON DELETE CASCADE,
    CONSTRAINT evidence_id_analysis_uq UNIQUE (id, analysis_id),
    CONSTRAINT evidence_location_uq UNIQUE (
        analysis_id, file_id, start_line, end_line, content_hash, extractor_version
    )
);

CREATE INDEX evidence_analysis_file_idx
    ON core.evidence (analysis_id, file_id, start_line);

CREATE TABLE core.entity_evidence (
    entity_id UUID NOT NULL REFERENCES core.entity(id) ON DELETE CASCADE,
    evidence_id UUID NOT NULL REFERENCES core.evidence(id) ON DELETE CASCADE,
    evidence_role TEXT NOT NULL DEFAULT 'DEFINITION',
    CONSTRAINT entity_evidence_role_ck CHECK (
        evidence_role IN ('DEFINITION', 'DECLARATION', 'USAGE', 'CONFIGURATION', 'INFERENCE_SUPPORT')
    ),
    PRIMARY KEY (entity_id, evidence_id, evidence_role)
);

CREATE TABLE core.repository_edge (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE CASCADE,
    source_entity_id UUID NOT NULL,
    target_entity_id UUID NOT NULL,
    relationship_type TEXT NOT NULL,
    certainty TEXT NOT NULL,
    confidence NUMERIC(5,4),
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT repository_edge_type_ck CHECK (
        relationship_type IN (
            'CONTAINS', 'DECLARES', 'IMPORTS', 'CALLS', 'EXTENDS', 'IMPLEMENTS',
            'EXPOSES', 'DEPENDS_ON', 'READS_FROM', 'WRITES_TO',
            'PUBLISHES_TO', 'CONSUMES_FROM'
        )
    ),
    CONSTRAINT repository_edge_certainty_ck CHECK (certainty IN ('EXACT', 'HIGH', 'MEDIUM', 'LOW')),
    CONSTRAINT repository_edge_confidence_ck CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    CONSTRAINT repository_edge_not_self_ck CHECK (source_entity_id <> target_entity_id),
    CONSTRAINT repository_edge_source_analysis_fk
        FOREIGN KEY (source_entity_id, analysis_id)
        REFERENCES core.entity (id, analysis_id)
        ON DELETE CASCADE,
    CONSTRAINT repository_edge_target_analysis_fk
        FOREIGN KEY (target_entity_id, analysis_id)
        REFERENCES core.entity (id, analysis_id)
        ON DELETE CASCADE,
    CONSTRAINT repository_edge_identity_uq UNIQUE (
        analysis_id, source_entity_id, relationship_type, target_entity_id, extractor_version
    ),
    CONSTRAINT repository_edge_id_analysis_uq UNIQUE (id, analysis_id)
);

CREATE INDEX repository_edge_source_type_idx
    ON core.repository_edge (analysis_id, source_entity_id, relationship_type);
CREATE INDEX repository_edge_target_type_idx
    ON core.repository_edge (analysis_id, target_entity_id, relationship_type);

CREATE TABLE core.edge_evidence (
    edge_id UUID NOT NULL REFERENCES core.repository_edge(id) ON DELETE CASCADE,
    evidence_id UUID NOT NULL REFERENCES core.evidence(id) ON DELETE CASCADE,
    PRIMARY KEY (edge_id, evidence_id)
);

COMMIT;

