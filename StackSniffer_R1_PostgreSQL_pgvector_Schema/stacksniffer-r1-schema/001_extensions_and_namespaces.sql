BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS semantic;
CREATE SCHEMA IF NOT EXISTS graphrag;
CREATE SCHEMA IF NOT EXISTS evaluation;
CREATE SCHEMA IF NOT EXISTS ml;

COMMENT ON SCHEMA core IS
    'Canonical repository identity, analysis lifecycle, structural IR, relationships, and evidence.';
COMMENT ON SCHEMA semantic IS
    'Versioned chunks, embedding profiles, and pgvector representations.';
COMMENT ON SCHEMA graphrag IS
    'Neo4j projection metadata, graph communities, summaries, query traces, answers, and citations.';
COMMENT ON SCHEMA evaluation IS
    'Frozen benchmark datasets, relevance judgments, evaluation runs, and metrics.';
COMMENT ON SCHEMA ml IS
    'Contrastive-pair snapshots, training runs, and model-adaptation lineage.';

COMMIT;

