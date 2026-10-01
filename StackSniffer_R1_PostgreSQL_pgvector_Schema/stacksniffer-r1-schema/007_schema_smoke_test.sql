BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'pgvector extension is missing';
    END IF;

    IF to_regclass('core.repository') IS NULL
       OR to_regclass('semantic.embedding') IS NULL
       OR to_regclass('graphrag.graph_snapshot') IS NULL
       OR to_regclass('evaluation.dataset') IS NULL
       OR to_regclass('ml.training_run') IS NULL THEN
        RAISE EXCEPTION 'One or more StackSniffer R1 tables are missing';
    END IF;
END;
$$;

WITH inserted_repository AS (
    INSERT INTO core.repository (
        canonical_key, provider, owner_name, repository_name, clone_url, default_branch
    ) VALUES (
        'github:example:smoke-test',
        'github',
        'example',
        'smoke-test',
        'https://github.com/example/smoke-test.git',
        'main'
    )
    RETURNING id
),
inserted_version AS (
    INSERT INTO core.repository_version (repository_id, commit_sha, requested_ref)
    SELECT id, repeat('a', 40), 'main'
      FROM inserted_repository
    RETURNING id
),
inserted_analysis AS (
    INSERT INTO core.analysis (repository_version_id, structural_pipeline_version, status)
    SELECT id, 'smoke-v1', 'INDEXING'
      FROM inserted_version
    RETURNING id, repository_version_id
),
inserted_file AS (
    INSERT INTO core.source_file (
        analysis_id, path, language, content_hash, size_bytes, parse_status
    )
    SELECT id, 'src/Smoke.java', 'java', 'smoke-content-hash', 42, 'PARSED'
      FROM inserted_analysis
    RETURNING id, analysis_id
),
inserted_entity AS (
    INSERT INTO core.entity (
        analysis_id, file_id, entity_type, name, qualified_name, stable_key,
        start_line, end_line, extractor_version
    )
    SELECT analysis_id, id, 'CLASS', 'Smoke', 'example.Smoke',
           'class:example.Smoke', 1, 3, 'tree-sitter-java-smoke-v1'
      FROM inserted_file
    RETURNING id, analysis_id, file_id
),
chunk_configuration AS (
    SELECT '{"profile_key":"smoke-chunks","profile_version":1}'::TEXT AS canonical
),
inserted_chunk_profile AS (
    INSERT INTO semantic.chunk_profile (
        profile_key, profile_version, chunking_strategy, tokenizer, max_tokens, overlap_policy,
        symbol_boundary_policy, included_entity_types, include_generated, include_vendored, code_version,
        canonical_configuration, configuration, configuration_fingerprint
    )
    SELECT 'smoke-chunks', 1, 'symbol', 'smoke', 512, '{"lines":0}'::jsonb, 'innermost_declaration',
           ARRAY['CLASS'], false, false, 'smoke-v1',
           canonical, canonical::jsonb, encode(sha256(convert_to(canonical, 'UTF8')), 'hex')
      FROM chunk_configuration
    RETURNING id
),
inserted_chunk AS (
    INSERT INTO semantic.chunk (
        analysis_id, repository_version_id, file_id, entity_id, chunk_profile_id, stable_chunk_key,
        content, content_hash, token_count, ordinal, start_line, end_line, is_generated, is_vendored
    )
    SELECT e.analysis_id, a.repository_version_id, e.file_id, e.id, p.id, 'class:example.Smoke@0',
           'class Smoke {}', encode(sha256(convert_to('class Smoke {}', 'UTF8')), 'hex'),
           4, 0, 1, 3, false, false
      FROM inserted_entity e
      JOIN inserted_analysis a ON a.id = e.analysis_id
     CROSS JOIN inserted_chunk_profile p
    RETURNING id, content_hash
),
embedding_configuration AS (
    SELECT '{"profile_key":"smoke-3072","profile_version":1}'::TEXT AS canonical
),
inserted_profile AS (
    INSERT INTO semantic.embedding_profile (
        profile_key, profile_version, provider, model_name, model_revision, dimension, distance_metric,
        normalization_policy, text_template, code_version,
        canonical_configuration, configuration, configuration_fingerprint
    )
    SELECT 'smoke-3072', 1, 'test', 'deterministic-fake', 'fake-1', 3072, 'COSINE', 'L2', '{text}', 'smoke-v1',
           canonical, canonical::jsonb, encode(sha256(convert_to(canonical, 'UTF8')), 'hex')
      FROM embedding_configuration
    RETURNING id
)
INSERT INTO semantic.embedding (chunk_id, embedding_profile_id, chunk_content_hash, embedding)
SELECT inserted_chunk.id,
       inserted_profile.id,
       inserted_chunk.content_hash,
       ('[1,' || repeat('0,', 3070) || '0]')::vector(3072)
  FROM inserted_chunk
 CROSS JOIN inserted_profile;

DO $$
DECLARE
    nearest_profile_id UUID;
    nearest_count INTEGER;
BEGIN
    SELECT id INTO nearest_profile_id
      FROM semantic.embedding_profile
     WHERE profile_key = 'smoke-3072';

    SELECT count(*)
      INTO nearest_count
      FROM (
          SELECT e.id
            FROM semantic.embedding e
           WHERE e.embedding_profile_id = nearest_profile_id
           ORDER BY e.embedding <=> ('[1,' || repeat('0,', 3070) || '0]')::vector(3072)
           LIMIT 1
      ) nearest;

    IF nearest_count <> 1 THEN
        RAISE EXCEPTION 'Vector nearest-neighbor smoke test failed';
    END IF;
END;
$$;

-- A vector of any other dimension cannot be stored.
DO $$
BEGIN
    BEGIN
        INSERT INTO semantic.embedding (chunk_id, embedding_profile_id, chunk_content_hash, embedding)
        SELECT chunk_id, embedding_profile_id, chunk_content_hash, '[1,0,0]'::vector
          FROM semantic.embedding
         LIMIT 1;
        RAISE EXCEPTION 'a 3-dimensional vector was accepted';
    EXCEPTION
        WHEN data_exception THEN NULL;  -- expected: expected 3072 dimensions, not 3
    END;
END;
$$;

ROLLBACK;
