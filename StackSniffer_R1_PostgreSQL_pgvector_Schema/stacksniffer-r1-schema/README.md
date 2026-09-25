# StackSniffer R1 PostgreSQL + pgvector migrations

These scripts implement the canonical PostgreSQL schema for the eight-week StackSniffer AI/ML portfolio build.

PostgreSQL owns canonical repository intelligence, semantic data, GraphRAG metadata, evaluations, and ML experiment lineage. Neo4j remains a rebuildable projection and must never become the only source of a structural fact.

## Hosted database

Create a Neon PostgreSQL 18 project named `stacksniffer-r1` in **AWS US West 2 (Oregon)** to colocate it with the west-Oregon backend. Name the database `stacksniffer` and leave Neon Auth disabled.

- Give the backend the pooled TLS connection string as `DATABASE_URL`.
- Give migrations the direct TLS connection string as `DATABASE_DIRECT_URL`.
- Run schema migrations from CI or a one-off release job, not concurrently from every application replica.
- Keep Neo4j credentials separate; Neo4j is a projection target, not the canonical store.

## Execution order

1. `001_extensions_and_namespaces.sql`
2. `002_core_repository_ir.sql`
3. `003_semantic_pgvector.sql`
4. `004_graphrag_runtime.sql`
5. `005_evaluation_and_ml.sql`
6. `006_optional_ann_indexes.sql`
7. `007_schema_smoke_test.sql`

Run migrations with stop-on-error enabled:

```bash
export DATABASE_DIRECT_URL='postgresql://...'
RUN_SCHEMA_SMOKE_TEST=1 ./run_migrations.sh
```

Apply each migration set once to a new database. The runner uses the direct Neon connection, stops on the first error, and optionally executes the smoke test. The smoke test runs inside a transaction and rolls back its fixture data. Never commit either Neon connection string.

## Vector-index policy

- Begin with exact cosine search for the frozen retrieval baseline.
- Add approximate indexes only after corpus size and latency justify them.
- `semantic.embedding.embedding` deliberately uses unbounded `vector` so several model dimensions can coexist.
- Call `semantic.create_profile_hnsw_index('<profile-key>')` after inserting the relevant embedding profile and vectors.
- Profiles up to 2,000 dimensions use `vector` HNSW indexing.
- Profiles from 2,001 through 4,000 dimensions use a `halfvec` expression index.

The application must use the same cast as the selected index when querying.
