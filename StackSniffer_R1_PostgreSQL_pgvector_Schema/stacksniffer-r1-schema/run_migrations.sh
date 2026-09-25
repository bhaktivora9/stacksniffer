#!/usr/bin/env bash
set -Eeuo pipefail

: "${DATABASE_DIRECT_URL:?Set DATABASE_DIRECT_URL to the direct Neon TLS connection string}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for migration in \
  "${script_dir}/001_extensions_and_namespaces.sql" \
  "${script_dir}/002_core_repository_ir.sql" \
  "${script_dir}/003_semantic_pgvector.sql" \
  "${script_dir}/004_graphrag_runtime.sql" \
  "${script_dir}/005_evaluation_and_ml.sql" \
  "${script_dir}/006_optional_ann_indexes.sql"; do
  printf 'Applying %s\n' "$(basename -- "${migration}")"
  psql "${DATABASE_DIRECT_URL}" -X -v ON_ERROR_STOP=1 -f "${migration}"
done

if [[ "${RUN_SCHEMA_SMOKE_TEST:-0}" == "1" ]]; then
  printf 'Running transactional schema smoke test\n'
  psql "${DATABASE_DIRECT_URL}" -X -v ON_ERROR_STOP=1 \
    -f "${script_dir}/007_schema_smoke_test.sql"
fi

printf 'StackSniffer R1 schema applied successfully\n'
