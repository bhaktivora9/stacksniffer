BEGIN;

CREATE OR REPLACE FUNCTION semantic.create_profile_hnsw_index(
    requested_profile_key TEXT,
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
    index_name TEXT;
    index_sql TEXT;
BEGIN
    IF hnsw_m <= 0 OR hnsw_ef_construction <= 0 THEN
        RAISE EXCEPTION 'HNSW parameters must be positive';
    END IF;

    SELECT id, dimension
      INTO selected_profile_id, selected_dimension
      FROM semantic.embedding_profile
     WHERE profile_key = requested_profile_key;

    IF selected_profile_id IS NULL THEN
        RAISE EXCEPTION 'Unknown embedding profile: %', requested_profile_key;
    END IF;

    index_name := format(
        'embedding_hnsw_%s',
        substr(md5(selected_profile_id::TEXT), 1, 12)
    );

    IF selected_dimension <= 2000 THEN
        index_sql := format(
            'CREATE INDEX IF NOT EXISTS %I '
            'ON semantic.embedding USING hnsw '
            '((embedding::vector(%s)) vector_cosine_ops) '
            'WITH (m = %s, ef_construction = %s) '
            'WHERE profile_id = %L::uuid',
            index_name,
            selected_dimension,
            hnsw_m,
            hnsw_ef_construction,
            selected_profile_id
        );
    ELSIF selected_dimension <= 4000 THEN
        index_sql := format(
            'CREATE INDEX IF NOT EXISTS %I '
            'ON semantic.embedding USING hnsw '
            '((embedding::halfvec(%s)) halfvec_cosine_ops) '
            'WITH (m = %s, ef_construction = %s) '
            'WHERE profile_id = %L::uuid',
            index_name,
            selected_dimension,
            hnsw_m,
            hnsw_ef_construction,
            selected_profile_id
        );
    ELSE
        RAISE EXCEPTION
            'Profile % has % dimensions; this helper supports HNSW through 4,000 dimensions',
            requested_profile_key,
            selected_dimension;
    END IF;

    EXECUTE index_sql;
    RETURN index_name;
END;
$$;

COMMENT ON FUNCTION semantic.create_profile_hnsw_index(TEXT, INTEGER, INTEGER) IS
    'Creates a profile-specific cosine HNSW index. Uses vector through 2,000 dimensions and halfvec through 4,000.';

COMMIT;

-- Example after data is loaded and the exact-search baseline is recorded:
-- SELECT semantic.create_profile_hnsw_index('code-encoder-base-768-v1');
-- SELECT semantic.create_profile_hnsw_index('gemini-code-3072-v1');

