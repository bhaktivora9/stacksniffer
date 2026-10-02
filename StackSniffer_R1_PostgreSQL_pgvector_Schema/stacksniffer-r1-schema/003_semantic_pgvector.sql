BEGIN;

-- Semantic layer: versioned chunk profiles, content-addressed chunks, versioned embedding
-- profiles and one current vector per chunk, profile and chunk content.
--
-- R1 stores every vector in one physical pgvector dimension, 3,072 (gemini-embedding-001 at its
-- native size). A vector of any other dimension cannot be stored; the application must fail
-- rather than truncate, pad or coerce it.

-- --- chunk profiles -----------------------------------------------------------------------------

CREATE TABLE semantic.chunk_profile (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_key TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    chunking_strategy TEXT NOT NULL,
    tokenizer TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    overlap_policy JSONB NOT NULL,
    symbol_boundary_policy TEXT NOT NULL,
    included_entity_types TEXT[] NOT NULL,
    include_generated BOOLEAN NOT NULL,
    include_vendored BOOLEAN NOT NULL,
    code_version TEXT NOT NULL,
    -- The exact canonical JSON the fingerprint is computed from; configuration is its parsed form.
    canonical_configuration TEXT NOT NULL,
    configuration JSONB NOT NULL,
    configuration_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chunk_profile_key_not_blank CHECK (btrim(profile_key) <> ''),
    CONSTRAINT chunk_profile_version_ck CHECK (profile_version > 0),
    CONSTRAINT chunk_profile_max_tokens_ck CHECK (max_tokens > 0),
    CONSTRAINT chunk_profile_entity_types_ck CHECK (
        cardinality(included_entity_types) > 0
        AND included_entity_types <@ ARRAY['CLASS', 'INTERFACE', 'FUNCTION', 'METHOD', 'FILE', 'CONFIGURATION',
                                           'DOCUMENTATION']::TEXT[]
    ),
    CONSTRAINT chunk_profile_configuration_ck CHECK (configuration = canonical_configuration::jsonb),
    CONSTRAINT chunk_profile_fingerprint_ck CHECK (
        configuration_fingerprint = encode(sha256(convert_to(canonical_configuration, 'UTF8')), 'hex')
    ),
    CONSTRAINT chunk_profile_key_version_uq UNIQUE (profile_key, profile_version),
    CONSTRAINT chunk_profile_fingerprint_uq UNIQUE (configuration_fingerprint),
    -- Lets evaluation runs record the profile and its fingerprint under one foreign key.
    CONSTRAINT chunk_profile_id_fingerprint_uq UNIQUE (id, configuration_fingerprint)
);

-- --- chunks -------------------------------------------------------------------------------------

CREATE TABLE semantic.chunk (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID NOT NULL,
    repository_version_id UUID NOT NULL,
    file_id UUID NOT NULL,
    entity_id UUID,
    chunk_profile_id UUID NOT NULL REFERENCES semantic.chunk_profile(id) ON DELETE RESTRICT,
    stable_chunk_key TEXT NOT NULL,
    -- METHOD/FUNCTION/CLASS/INTERFACE: a declaration; CODE: file-level code outside one;
    -- CONFIGURATION and DOCUMENTATION: config and docs files. entity_id is the declaration, else the FILE entity.
    chunk_kind TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_byte INTEGER,
    end_byte INTEGER,
    is_generated BOOLEAN NOT NULL,
    is_vendored BOOLEAN NOT NULL,
    -- Default retrieval filters on these; both are derived, never supplied independently.
    is_first_party BOOLEAN NOT NULL,
    language TEXT,  -- copied from the source file on insert
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chunk_key_not_blank CHECK (btrim(stable_chunk_key) <> ''),
    CONSTRAINT chunk_kind_ck CHECK (
        chunk_kind IN ('METHOD', 'FUNCTION', 'CLASS', 'INTERFACE', 'CODE', 'CONFIGURATION', 'DOCUMENTATION')
    ),
    CONSTRAINT chunk_first_party_ck CHECK (is_first_party = NOT (is_generated OR is_vendored)),
    CONSTRAINT chunk_content_not_blank CHECK (btrim(content) <> ''),
    CONSTRAINT chunk_content_hash_ck CHECK (
        content_hash = encode(sha256(convert_to(content, 'UTF8')), 'hex')
    ),
    CONSTRAINT chunk_token_count_ck CHECK (token_count > 0),
    CONSTRAINT chunk_ordinal_ck CHECK (ordinal >= 0),
    CONSTRAINT chunk_line_range_ck CHECK (start_line > 0 AND end_line >= start_line),
    CONSTRAINT chunk_byte_range_ck CHECK (
        (start_byte IS NULL AND end_byte IS NULL)
        OR (start_byte >= 0 AND end_byte > start_byte)
    ),
    -- The repository version is the analysis's own, not a free-standing copy.
    CONSTRAINT chunk_analysis_version_fk
        FOREIGN KEY (analysis_id, repository_version_id)
        REFERENCES core.analysis (id, repository_version_id)
        ON DELETE CASCADE,
    -- The file belongs to the same analysis, and the origin flags are the file's.
    CONSTRAINT chunk_file_origin_fk
        FOREIGN KEY (file_id, analysis_id, is_generated, is_vendored)
        REFERENCES core.source_file (id, analysis_id, is_generated, is_vendored)
        ON DELETE CASCADE,
    CONSTRAINT chunk_entity_analysis_fk
        FOREIGN KEY (entity_id, analysis_id)
        REFERENCES core.entity (id, analysis_id)
        ON DELETE CASCADE,
    -- Repeated indexing of the same analysis and profile reuses the chunk.
    CONSTRAINT chunk_identity_uq UNIQUE (analysis_id, chunk_profile_id, stable_chunk_key, content_hash),
    CONSTRAINT chunk_id_analysis_uq UNIQUE (id, analysis_id),
    CONSTRAINT chunk_id_content_uq UNIQUE (id, content_hash),
    CONSTRAINT chunk_id_content_origin_uq UNIQUE (id, content_hash, is_first_party)
);

-- Chunks of an analysis under a profile, in document order.
CREATE INDEX chunk_analysis_profile_idx
    ON semantic.chunk (analysis_id, chunk_profile_id, ordinal);
-- Metadata filters for retrieval: first-party origin and language.
CREATE INDEX chunk_analysis_profile_filter_idx
    ON semantic.chunk (analysis_id, chunk_profile_id, is_first_party, language);
CREATE INDEX chunk_entity_idx
    ON semantic.chunk (entity_id);
-- Chunks covering a source range.
CREATE INDEX chunk_file_range_idx
    ON semantic.chunk (file_id, start_line, end_line);
CREATE INDEX chunk_profile_idx
    ON semantic.chunk (chunk_profile_id);

-- Why a file produced no chunks under a profile, recorded so exclusions stay explainable.
CREATE TABLE semantic.chunk_exclusion (
    analysis_id UUID NOT NULL,
    chunk_profile_id UUID NOT NULL REFERENCES semantic.chunk_profile(id) ON DELETE RESTRICT,
    file_id UUID NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (analysis_id, chunk_profile_id, file_id),
    CONSTRAINT chunk_exclusion_file_fk
        FOREIGN KEY (file_id, analysis_id) REFERENCES core.source_file (id, analysis_id) ON DELETE CASCADE,
    CONSTRAINT chunk_exclusion_reason_ck CHECK (reason IN (
        'GENERATED', 'VENDORED', 'SECRET', 'BINARY', 'OVERSIZED', 'CONTENT_NOT_RETAINED', 'NOT_EXTRACTED',
        'KIND_NOT_INCLUDED', 'NO_INDEXABLE_CONTENT'
    ))
);

CREATE TABLE semantic.chunk_entity (
    chunk_id UUID NOT NULL REFERENCES semantic.chunk(id) ON DELETE CASCADE,
    entity_id UUID NOT NULL REFERENCES core.entity(id) ON DELETE CASCADE,
    entity_role TEXT NOT NULL DEFAULT 'MENTIONED',
    CONSTRAINT chunk_entity_role_ck CHECK (entity_role IN ('PRIMARY', 'MENTIONED', 'STRUCTURAL_NEIGHBOR')),
    PRIMARY KEY (chunk_id, entity_id, entity_role)
);

CREATE INDEX chunk_entity_entity_idx
    ON semantic.chunk_entity (entity_id);

-- --- embedding profiles -------------------------------------------------------------------------

CREATE TABLE semantic.embedding_profile (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_key TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'RETRIEVAL',
    dimension INTEGER NOT NULL,
    distance_metric TEXT NOT NULL,
    normalization_policy TEXT NOT NULL,
    document_task TEXT,
    query_task TEXT,
    text_template TEXT NOT NULL,
    pooling_strategy TEXT,
    code_version TEXT NOT NULL,
    canonical_configuration TEXT NOT NULL,
    configuration JSONB NOT NULL,
    configuration_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CANDIDATE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    CONSTRAINT embedding_profile_key_not_blank CHECK (btrim(profile_key) <> ''),
    CONSTRAINT embedding_profile_version_ck CHECK (profile_version > 0),
    CONSTRAINT embedding_profile_revision_not_blank CHECK (btrim(model_revision) <> ''),
    CONSTRAINT embedding_profile_purpose_ck CHECK (
        purpose IN ('RETRIEVAL', 'CLUSTERING', 'TRAINING_BASELINE', 'EXPERIMENT')
    ),
    -- The single physical dimension of semantic.embedding.embedding in R1.
    CONSTRAINT embedding_profile_dimension_ck CHECK (dimension = 3072),
    CONSTRAINT embedding_profile_metric_ck CHECK (distance_metric IN ('COSINE', 'L2', 'INNER_PRODUCT')),
    CONSTRAINT embedding_profile_normalization_ck CHECK (normalization_policy IN ('NONE', 'L2')),
    CONSTRAINT embedding_profile_template_ck CHECK (position('{text}' IN text_template) > 0),
    CONSTRAINT embedding_profile_status_ck CHECK (status IN ('CANDIDATE', 'ACTIVE', 'REJECTED', 'RETIRED')),
    CONSTRAINT embedding_profile_configuration_ck CHECK (configuration = canonical_configuration::jsonb),
    CONSTRAINT embedding_profile_fingerprint_ck CHECK (
        configuration_fingerprint = encode(sha256(convert_to(canonical_configuration, 'UTF8')), 'hex')
    ),
    CONSTRAINT embedding_profile_key_version_uq UNIQUE (profile_key, profile_version),
    CONSTRAINT embedding_profile_fingerprint_uq UNIQUE (configuration_fingerprint),
    CONSTRAINT embedding_profile_id_fingerprint_uq UNIQUE (id, configuration_fingerprint)
);

CREATE UNIQUE INDEX embedding_profile_one_active_per_purpose_uq
    ON semantic.embedding_profile (purpose)
    WHERE status = 'ACTIVE';

-- --- embeddings ---------------------------------------------------------------------------------

CREATE TABLE semantic.embedding (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id UUID NOT NULL,
    embedding_profile_id UUID NOT NULL REFERENCES semantic.embedding_profile(id) ON DELETE RESTRICT,
    -- The chunk content the vector was computed from; the foreign key makes it the chunk's own.
    chunk_content_hash TEXT NOT NULL,
    -- The chunk's origin, carried here so the default HNSW index can be partial (first-party only).
    is_first_party BOOLEAN NOT NULL,
    embedding vector(3072) NOT NULL,
    response_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT embedding_chunk_content_fk
        FOREIGN KEY (chunk_id, chunk_content_hash, is_first_party)
        REFERENCES semantic.chunk (id, content_hash, is_first_party)
        ON DELETE CASCADE,
    CONSTRAINT embedding_current_uq UNIQUE (chunk_id, embedding_profile_id, chunk_content_hash)
);

CREATE INDEX embedding_profile_chunk_idx
    ON semantic.embedding (embedding_profile_id, chunk_id);

COMMENT ON COLUMN semantic.embedding.embedding IS
    'R1 physical dimension 3,072. HNSW indexing uses a halfvec(3072) expression index per profile (006).';

-- --- immutability -------------------------------------------------------------------------------

-- A chunk profile is immutable once a chunk uses it. Chunk inserts take a share lock on the
-- profile row so a concurrent profile update waits and then sees the chunk.
CREATE OR REPLACE FUNCTION semantic.lock_chunk_profile()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = semantic, public, pg_temp
AS $$
BEGIN
    PERFORM 1 FROM semantic.chunk_profile WHERE id = NEW.chunk_profile_id FOR SHARE;
    -- Derived filter columns: the file's language; first-party from the origin flags.
    SELECT language INTO NEW.language
      FROM core.source_file WHERE id = NEW.file_id AND analysis_id = NEW.analysis_id;
    NEW.is_first_party := NOT (NEW.is_generated OR NEW.is_vendored);
    RETURN NEW;
END;
$$;

CREATE TRIGGER chunk_locks_profile_trg
BEFORE INSERT ON semantic.chunk
FOR EACH ROW EXECUTE FUNCTION semantic.lock_chunk_profile();

CREATE OR REPLACE FUNCTION semantic.forbid_used_chunk_profile_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = semantic, public, pg_temp
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM semantic.chunk WHERE chunk_profile_id = OLD.id) THEN
        RAISE EXCEPTION 'chunk profile %/% is used by chunks and is immutable',
            OLD.profile_key, OLD.profile_version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER chunk_profile_immutable_trg
BEFORE UPDATE ON semantic.chunk_profile
FOR EACH ROW EXECUTE FUNCTION semantic.forbid_used_chunk_profile_change();

-- Chunks are content-addressed: a change is a new chunk, never an update.
CREATE OR REPLACE FUNCTION semantic.forbid_chunk_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = semantic, public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'semantic.chunk rows are immutable' USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

CREATE TRIGGER chunk_immutable_trg
BEFORE UPDATE ON semantic.chunk
FOR EACH ROW EXECUTE FUNCTION semantic.forbid_chunk_update();

-- An embedding profile is immutable once a vector uses it, except for its lifecycle status.
CREATE OR REPLACE FUNCTION semantic.lock_embedding_profile()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = semantic, public, pg_temp
AS $$
BEGIN
    PERFORM 1 FROM semantic.embedding_profile WHERE id = NEW.embedding_profile_id FOR SHARE;
    IF NEW.is_first_party IS NULL THEN
        SELECT is_first_party INTO NEW.is_first_party FROM semantic.chunk WHERE id = NEW.chunk_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER embedding_locks_profile_trg
BEFORE INSERT ON semantic.embedding
FOR EACH ROW EXECUTE FUNCTION semantic.lock_embedding_profile();

CREATE OR REPLACE FUNCTION semantic.forbid_used_embedding_profile_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = semantic, public, pg_temp
AS $$
BEGIN
    IF (to_jsonb(NEW) - 'status' - 'activated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'activated_at')
       AND EXISTS (SELECT 1 FROM semantic.embedding WHERE embedding_profile_id = OLD.id) THEN
        RAISE EXCEPTION 'embedding profile %/% is used by embeddings; only its status may change',
            OLD.profile_key, OLD.profile_version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER embedding_profile_immutable_trg
BEFORE UPDATE ON semantic.embedding_profile
FOR EACH ROW EXECUTE FUNCTION semantic.forbid_used_embedding_profile_change();

COMMIT;
