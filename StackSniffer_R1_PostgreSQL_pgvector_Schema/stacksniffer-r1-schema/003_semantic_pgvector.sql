BEGIN;

CREATE TABLE semantic.chunk (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL REFERENCES core.analysis(id) ON DELETE CASCADE,
    file_id UUID NOT NULL,
    primary_entity_id UUID,
    chunk_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    token_count INTEGER,
    chunking_strategy TEXT NOT NULL,
    semantic_schema_version TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chunk_type_ck CHECK (
        chunk_type IN ('METHOD', 'FUNCTION', 'CLASS', 'INTERFACE', 'CONFIGURATION', 'DOCUMENTATION', 'FILE_SUMMARY')
    ),
    CONSTRAINT chunk_content_not_blank CHECK (btrim(content) <> ''),
    CONSTRAINT chunk_line_range_ck CHECK (start_line > 0 AND end_line >= start_line),
    CONSTRAINT chunk_token_count_ck CHECK (token_count IS NULL OR token_count >= 0),
    CONSTRAINT chunk_file_analysis_fk
        FOREIGN KEY (file_id, analysis_id)
        REFERENCES core.source_file (id, analysis_id)
        ON DELETE CASCADE,
    CONSTRAINT chunk_entity_analysis_fk
        FOREIGN KEY (primary_entity_id, analysis_id)
        REFERENCES core.entity (id, analysis_id)
        ON DELETE RESTRICT,
    CONSTRAINT chunk_identity_uq UNIQUE (
        analysis_id, content_hash, chunking_strategy, semantic_schema_version
    ),
    CONSTRAINT chunk_id_analysis_uq UNIQUE (id, analysis_id)
);

CREATE INDEX chunk_analysis_type_idx
    ON semantic.chunk (analysis_id, chunk_type);
CREATE INDEX chunk_analysis_file_idx
    ON semantic.chunk (analysis_id, file_id);
CREATE INDEX chunk_primary_entity_idx
    ON semantic.chunk (primary_entity_id);

CREATE TABLE semantic.chunk_entity (
    chunk_id UUID NOT NULL REFERENCES semantic.chunk(id) ON DELETE CASCADE,
    entity_id UUID NOT NULL REFERENCES core.entity(id) ON DELETE CASCADE,
    entity_role TEXT NOT NULL DEFAULT 'MENTIONED',
    CONSTRAINT chunk_entity_role_ck CHECK (entity_role IN ('PRIMARY', 'MENTIONED', 'STRUCTURAL_NEIGHBOR')),
    PRIMARY KEY (chunk_id, entity_id, entity_role)
);

CREATE TABLE semantic.embedding_profile (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_revision TEXT,
    purpose TEXT NOT NULL DEFAULT 'RETRIEVAL',
    dimension INTEGER NOT NULL,
    distance_metric TEXT NOT NULL DEFAULT 'COSINE',
    pooling_strategy TEXT,
    status TEXT NOT NULL DEFAULT 'CANDIDATE',
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    CONSTRAINT embedding_profile_key_not_blank CHECK (btrim(profile_key) <> ''),
    CONSTRAINT embedding_profile_purpose_ck CHECK (
        purpose IN ('RETRIEVAL', 'CLUSTERING', 'TRAINING_BASELINE', 'EXPERIMENT')
    ),
    CONSTRAINT embedding_profile_dimension_ck CHECK (dimension > 0 AND dimension <= 16000),
    CONSTRAINT embedding_profile_metric_ck CHECK (distance_metric IN ('COSINE', 'L2', 'INNER_PRODUCT')),
    CONSTRAINT embedding_profile_status_ck CHECK (status IN ('CANDIDATE', 'ACTIVE', 'REJECTED', 'RETIRED'))
);

CREATE UNIQUE INDEX embedding_profile_one_active_per_purpose_uq
    ON semantic.embedding_profile (purpose)
    WHERE status = 'ACTIVE';

CREATE TABLE semantic.embedding (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id UUID NOT NULL REFERENCES semantic.chunk(id) ON DELETE CASCADE,
    profile_id UUID NOT NULL REFERENCES semantic.embedding_profile(id) ON DELETE RESTRICT,
    dimension INTEGER NOT NULL,
    embedding vector NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT embedding_dimension_ck CHECK (dimension > 0 AND vector_dims(embedding) = dimension),
    CONSTRAINT embedding_chunk_profile_uq UNIQUE (chunk_id, profile_id)
);

CREATE INDEX embedding_profile_idx
    ON semantic.embedding (profile_id);
CREATE INDEX embedding_chunk_idx
    ON semantic.embedding (chunk_id);
CREATE INDEX embedding_profile_dimension_idx
    ON semantic.embedding (profile_id, dimension);

CREATE OR REPLACE FUNCTION semantic.validate_embedding_profile_dimension()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = semantic, public, pg_temp
AS $$
DECLARE
    expected_dimension INTEGER;
BEGIN
    SELECT dimension
      INTO expected_dimension
      FROM semantic.embedding_profile
     WHERE id = NEW.profile_id;

    IF expected_dimension IS NULL THEN
        RAISE EXCEPTION 'Embedding profile % does not exist', NEW.profile_id;
    END IF;

    IF NEW.dimension <> expected_dimension OR vector_dims(NEW.embedding) <> expected_dimension THEN
        RAISE EXCEPTION
            'Embedding dimension % does not match profile dimension %',
            vector_dims(NEW.embedding), expected_dimension;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER embedding_profile_dimension_trg
BEFORE INSERT OR UPDATE OF profile_id, dimension, embedding
ON semantic.embedding
FOR EACH ROW
EXECUTE FUNCTION semantic.validate_embedding_profile_dimension();

COMMENT ON COLUMN semantic.embedding.embedding IS
    'Unbounded vector permits multiple model dimensions. Use profile-specific partial expression indexes.';

COMMIT;
