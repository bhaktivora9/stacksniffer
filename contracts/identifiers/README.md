# Canonical identifiers

All identifier values are serialized as JSON strings using these canonical forms:

| Identifier | Canonical representation |
| --- | --- |
| `repository_key` | `provider/owner/repository`, lower-case; provider is `[a-z][a-z0-9-]{1,31}` and owner/repository are `[a-z0-9][a-z0-9_.-]{0,99}` |
| `repository_version_id` | Lower-case RFC 4122 UUID string |
| `analysis_id` | Lower-case RFC 4122 UUID string |
| `analysis_job_id` | Lower-case RFC 4122 UUID string |
| `classification_run_id` | Lower-case RFC 4122 UUID string |
| `classification_result_id` | Lower-case RFC 4122 UUID string |
| `agent_run_id` | Lower-case RFC 4122 UUID string |
| `event_id` | Lower-case RFC 4122 UUID string |
| `request_id` | Lower-case RFC 4122 UUID string |
| `correlation_id` | Lower-case RFC 4122 UUID string |
| `taxonomy_version` | `vMAJOR.MINOR.PATCH`, with non-negative decimal components and no leading zeroes except `0` |

Generated identifiers are UUID version 4 by the bindings. Deserialization accepts only the
canonical lowercase textual form; this prevents multiple wire representations of the same ID.

The JSON representation of an identifier bundle uses the exact field names above. Unknown fields
are rejected and all fields are required. See `../golden-vectors/identifiers.json` for shared
valid and invalid examples.

## Derived identities

Derived identities are `sha256:<64 lowercase hexadecimal characters>`. The digest input is a
UTF-8, compact JSON object with lexicographically sorted keys and a `namespace` field. Only the
inputs listed below are included:

| Identity | Determining inputs |
| --- | --- |
| `DeterministicAnalysisKey` | `repository_key`, `commit_sha`, `deterministic_pipeline_version` |
| `ClassificationContractId` | `classification_pipeline_version`, `classification_model_profile`, `prompt_version`, `taxonomy_version` |
| `ClassificationKey` | `analysis_id`, `classification_contract_id` |
| `AssembledAnalysisKey` | `deterministic_analysis_key`, `classification_key` |
| `AgentReviewSpecKey` | `repository_key`, `commit_sha`, `review_goal`, `agent_version`, `prompt_version`, `model_profile` |
| `AgentContextFingerprint` | `classification_result_id`, `retrieval_index_version`, `taxonomy_version`, `graph_snapshot_version` |
| `AgentResultCacheKey` | `agent_review_spec_key`, `agent_context_fingerprint` |

`graph_snapshot_version` is the literal `NONE` when graph context is unavailable. `agent_run_id`
is intentionally not an input to `AgentResultCacheKey`; a retry with a new run ID resolves to the
same cached result. Changing taxonomy changes the classification contract, classification key,
and assembled key, but does not change the deterministic analysis key.

The derivation implementations and cross-language vectors are in
`python/canonical_identifiers.py`, `java/CanonicalIdentifiers.java`, and
`../golden-vectors/derivations.json`.
