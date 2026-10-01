BEGIN;

-- R1 vectors are vector(3072). pgvector's HNSW supports `vector` only through 2,000 dimensions,
-- so the approximate index is a halfvec(3072) expression index, one partial index per profile.
-- Default retrieval excludes generated and vendored code, so the default index covers
-- first-party chunks only. Queries must repeat the index's expression and predicate:
--   WHERE e.embedding_profile_id = $profile AND e.is_first_party
--   ORDER BY e.embedding::halfvec(3072) <=> $query::halfvec(3072)
CREATE OR REPLACE FUNCTION semantic.create_profile_hnsw_index(
    requested_profile_key TEXT,
    requested_profile_version INTEGER,
    first_party_only BOOLEAN DEFAULT true,
    hnsw_m INTEGER DEFAULT 16,
    hnsw_ef_construction INTEGER DEFAULT 64
)
RETURNS TEXT
LANGUAGE plpgsql
SET search_path = semantic, public, pg_temp
AS $$
DECLARE
    selected_profile_id UUID;
    selected_dimension INTEGER;
    selected_metric TEXT;
    operator_class TEXT;
    index_name TEXT;
BEGIN
    IF hnsw_m <= 0 OR hnsw_ef_construction <= 0 THEN
        RAISE EXCEPTION 'HNSW parameters must be positive';
    END IF;

    SELECT id, dimension, distance_metric
      INTO selected_profile_id, selected_dimension, selected_metric
      FROM semantic.embedding_profile
     WHERE profile_key = requested_profile_key
       AND profile_version = requested_profile_version;

    IF selected_profile_id IS NULL THEN
        RAISE EXCEPTION 'Unknown embedding profile: %/%', requested_profile_key, requested_profile_version;
    END IF;

    operator_class := CASE selected_metric
        WHEN 'COSINE' THEN 'halfvec_cosine_ops'
        WHEN 'L2' THEN 'halfvec_l2_ops'
        ELSE 'halfvec_ip_ops'
    END;
    index_name := format('embedding_hnsw_%s%s', substr(md5(selected_profile_id::TEXT), 1, 12),
                         CASE WHEN first_party_only THEN '_fp' ELSE '_all' END);

    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS %I '
        'ON semantic.embedding USING hnsw ((embedding::halfvec(%s)) %s) '
        'WITH (m = %s, ef_construction = %s) '
        'WHERE embedding_profile_id = %L::uuid%s',
        index_name, selected_dimension, operator_class, hnsw_m, hnsw_ef_construction, selected_profile_id,
        CASE WHEN first_party_only THEN ' AND is_first_party' ELSE '' END
    );
    RETURN index_name;
END;
$$;

COMMENT ON FUNCTION semantic.create_profile_hnsw_index(TEXT, INTEGER, BOOLEAN, INTEGER, INTEGER) IS
    'Creates a per-profile halfvec(3072) HNSW index with the operator class of the profile''s metric; '
    'first-party chunks only by default.';

COMMIT;

-- Example after data is loaded and the exact-search baseline is recorded:
-- SELECT semantic.create_profile_hnsw_index('gemini-embedding-001-3072', 1);
