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
    RETURNING id
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
inserted_chunk AS (
    INSERT INTO semantic.chunk (
        analysis_id, file_id, primary_entity_id, chunk_type, content, content_hash,
        start_line, end_line, token_count, chunking_strategy, semantic_schema_version
    )
    SELECT analysis_id, file_id, id, 'CLASS', 'class Smoke {}', 'smoke-chunk-hash',
           1, 3, 4, 'symbol-aware-smoke-v1', 'v1'
      FROM inserted_entity
    RETURNING id
),
inserted_profile AS (
    INSERT INTO semantic.embedding_profile (
        profile_key, provider, model_name, dimension, distance_metric, status
    ) VALUES (
        'smoke-3d-v1', 'test', 'deterministic-fake', 3, 'COSINE', 'CANDIDATE'
    )
    RETURNING id
)
INSERT INTO semantic.embedding (chunk_id, profile_id, dimension, embedding, content_hash)
SELECT inserted_chunk.id,
       inserted_profile.id,
       3,
       '[1,0,0]'::vector,
       'smoke-chunk-hash'
  FROM inserted_chunk
 CROSS JOIN inserted_profile;

DO $$
DECLARE
    nearest_profile_id UUID;
    nearest_count INTEGER;
BEGIN
    SELECT id INTO nearest_profile_id
      FROM semantic.embedding_profile
     WHERE profile_key = 'smoke-3d-v1';

    SELECT count(*)
      INTO nearest_count
      FROM (
          SELECT e.id
            FROM semantic.embedding e
           WHERE e.profile_id = nearest_profile_id
           ORDER BY e.embedding <=> '[1,0,0]'::vector
           LIMIT 1
      ) nearest;

    IF nearest_count <> 1 THEN
        RAISE EXCEPTION 'Vector nearest-neighbor smoke test failed';
    END IF;
END;
$$;

ROLLBACK;
