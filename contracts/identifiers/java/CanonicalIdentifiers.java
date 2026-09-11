package io.stacksniffer.contracts.identifiers;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import java.util.regex.Pattern;

/** Canonical StackSniffer identifier bindings and JSON-field serialization. */
public final class CanonicalIdentifiers {
    private static final Pattern UUID_PATTERN = Pattern.compile(
            "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");
    private static final Pattern REPOSITORY_KEY_PATTERN = Pattern.compile(
            "^[a-z][a-z0-9-]{1,31}/[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9][a-z0-9_.-]{0,99}$");
    private static final Pattern TAXONOMY_VERSION_PATTERN = Pattern.compile(
            "^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$");

    private CanonicalIdentifiers() {
    }

    public record Bundle(
            String repository_key,
            String repository_version_id,
            String analysis_id,
            String analysis_job_id,
            String classification_run_id,
            String classification_result_id,
            String agent_run_id,
            String event_id,
            String request_id,
            String correlation_id,
            String taxonomy_version) {
        public Bundle {
            validateRepositoryKey(repository_key);
            validateUuid("repository_version_id", repository_version_id);
            validateUuid("analysis_id", analysis_id);
            validateUuid("analysis_job_id", analysis_job_id);
            validateUuid("classification_run_id", classification_run_id);
            validateUuid("classification_result_id", classification_result_id);
            validateUuid("agent_run_id", agent_run_id);
            validateUuid("event_id", event_id);
            validateUuid("request_id", request_id);
            validateUuid("correlation_id", correlation_id);
            validateTaxonomyVersion(taxonomy_version);
        }

        public static Bundle create(String repositoryKey, String taxonomyVersion) {
            return new Bundle(repositoryKey, uuid(), uuid(), uuid(), uuid(), uuid(), uuid(), uuid(), uuid(), uuid(), taxonomyVersion);
        }

        public Map<String, String> toMap() {
            Map<String, String> values = new LinkedHashMap<>();
            values.put("repository_key", repository_key);
            values.put("repository_version_id", repository_version_id);
            values.put("analysis_id", analysis_id);
            values.put("analysis_job_id", analysis_job_id);
            values.put("classification_run_id", classification_run_id);
            values.put("classification_result_id", classification_result_id);
            values.put("agent_run_id", agent_run_id);
            values.put("event_id", event_id);
            values.put("request_id", request_id);
            values.put("correlation_id", correlation_id);
            values.put("taxonomy_version", taxonomy_version);
            return Map.copyOf(values);
        }

        public String toJson() {
            StringBuilder json = new StringBuilder("{");
            boolean first = true;
            for (Map.Entry<String, String> entry : toMap().entrySet()) {
                if (!first) {
                    json.append(',');
                }
                json.append('"').append(entry.getKey()).append("\":\"").append(entry.getValue()).append('"');
                first = false;
            }
            return json.append('}').toString();
        }

        /** Reconstruct a bundle from a decoded JSON object represented as a map. */
        public static Bundle fromMap(Map<String, ?> values) {
            if (values == null || values.size() != 11
                    || !values.keySet().equals(SetNames.ALL)) {
                throw new IllegalArgumentException("identifier payload must contain exactly the canonical fields");
            }
            return new Bundle(
                    stringValue(values, "repository_key"),
                    stringValue(values, "repository_version_id"),
                    stringValue(values, "analysis_id"),
                    stringValue(values, "analysis_job_id"),
                    stringValue(values, "classification_run_id"),
                    stringValue(values, "classification_result_id"),
                    stringValue(values, "agent_run_id"),
                    stringValue(values, "event_id"),
                    stringValue(values, "request_id"),
                    stringValue(values, "correlation_id"),
                    stringValue(values, "taxonomy_version"));
        }

        public static Bundle fromJson(String json) {
            if (json == null || !json.startsWith("{") || !json.endsWith("}")) {
                throw new IllegalArgumentException("identifier payload must be a JSON object");
            }
            Map<String, String> values = new LinkedHashMap<>();
            String body = json.substring(1, json.length() - 1);
            if (!body.isEmpty()) {
                for (String member : body.split(",", -1)) {
                    String[] pair = member.split(":", 2);
                    if (pair.length != 2 || pair[0].length() < 2 || pair[1].length() < 2
                            || pair[0].charAt(0) != '"' || pair[0].charAt(pair[0].length() - 1) != '"'
                            || pair[1].charAt(0) != '"' || pair[1].charAt(pair[1].length() - 1) != '"') {
                        throw new IllegalArgumentException("identifier payload must contain JSON strings");
                    }
                    values.put(pair[0].substring(1, pair[0].length() - 1),
                            pair[1].substring(1, pair[1].length() - 1));
                }
            }
            return fromMap(values);
        }
    }

    private static final class SetNames {
        private static final java.util.Set<String> ALL = java.util.Set.of(
                "repository_key", "repository_version_id", "analysis_id", "analysis_job_id",
                "classification_run_id", "classification_result_id", "agent_run_id", "event_id",
                "request_id", "correlation_id", "taxonomy_version");
    }

    public static String validateRepositoryKey(String value) {
        if (value == null || !REPOSITORY_KEY_PATTERN.matcher(value).matches()) {
            throw new IllegalArgumentException("repository_key must be provider/owner/repository in lowercase");
        }
        return value;
    }

    public static String validateTaxonomyVersion(String value) {
        if (value == null || !TAXONOMY_VERSION_PATTERN.matcher(value).matches()) {
            throw new IllegalArgumentException("taxonomy_version must use vMAJOR.MINOR.PATCH");
        }
        return value;
    }

    public static String validateUuid(String fieldName, String value) {
        if (value == null || !UUID_PATTERN.matcher(value).matches()) {
            throw new IllegalArgumentException(fieldName + " must be a canonical lowercase UUID");
        }
        try {
            UUID.fromString(value);
        } catch (IllegalArgumentException exception) {
            throw new IllegalArgumentException(fieldName + " must be a valid UUID", exception);
        }
        return value;
    }

    public static String deterministicAnalysisKey(String repositoryKey, String commitSha,
                                                  String deterministicPipelineVersion) {
        validateRepositoryKey(repositoryKey);
        validateCommitSha(commitSha);
        validateText("deterministic_pipeline_version", deterministicPipelineVersion);
        return derive("deterministic_analysis", Map.of(
                "commit_sha", commitSha,
                "deterministic_pipeline_version", deterministicPipelineVersion,
                "repository_key", repositoryKey));
    }

    public static String classificationContractId(String classificationPipelineVersion,
                                                  String classificationModelProfile,
                              String promptVersion,
                                                  String taxonomyVersion) {
        validateText("classification_pipeline_version", classificationPipelineVersion);
        validateText("classification_model_profile", classificationModelProfile);
        validateText("prompt_version", promptVersion);
        validateTaxonomyVersion(taxonomyVersion);
        return derive("classification_contract", Map.of(
                "classification_model_profile", classificationModelProfile,
                "classification_pipeline_version", classificationPipelineVersion,
            "prompt_version", promptVersion,
                "taxonomy_version", taxonomyVersion));
    }

    public static String classificationKey(String analysisId, String contractId) {
        validateUuid("analysis_id", analysisId);
        validateText("classification_contract_id", contractId);
        return derive("classification", Map.of(
                "analysis_id", analysisId,
                "classification_contract_id", contractId));
    }

    public static String assembledAnalysisKey(String deterministicKey, String classificationKey) {
        validateText("deterministic_analysis_key", deterministicKey);
        validateText("classification_key", classificationKey);
        return derive("assembled_analysis", Map.of(
                "classification_key", classificationKey,
                "deterministic_analysis_key", deterministicKey));
    }

    public static String agentReviewSpecKey(String repositoryKey, String commitSha, String reviewGoal,
                                            String agentVersion, String promptVersion, String modelProfile) {
        validateRepositoryKey(repositoryKey);
        validateCommitSha(commitSha);
        validateText("review_goal", reviewGoal);
        validateText("agent_version", agentVersion);
        validateText("prompt_version", promptVersion);
        validateText("model_profile", modelProfile);
        return derive("agent_review_spec", Map.of(
                "agent_version", agentVersion,
                "commit_sha", commitSha,
                "model_profile", modelProfile,
                "prompt_version", promptVersion,
                "repository_key", repositoryKey,
                "review_goal", reviewGoal));
    }

    public static String agentContextFingerprint(String classificationResultId, String retrievalIndexVersion,
                                                 String taxonomyVersion, String graphSnapshotVersion) {
        validateUuid("classification_result_id", classificationResultId);
        validateText("retrieval_index_version", retrievalIndexVersion);
        validateTaxonomyVersion(taxonomyVersion);
        validateText("graph_snapshot_version", graphSnapshotVersion);
        return derive("agent_context", Map.of(
                "classification_result_id", classificationResultId,
                "graph_snapshot_version", graphSnapshotVersion,
                "retrieval_index_version", retrievalIndexVersion,
                "taxonomy_version", taxonomyVersion));
    }

    public static String agentResultCacheKey(String reviewSpecKey, String contextFingerprint) {
        validateText("agent_review_spec_key", reviewSpecKey);
        validateText("agent_context_fingerprint", contextFingerprint);
        return derive("agent_result_cache", Map.of(
                "agent_context_fingerprint", contextFingerprint,
                "agent_review_spec_key", reviewSpecKey));
    }

    private static String validateCommitSha(String value) {
        if (value == null || !value.matches("[0-9a-f]{7,64}")) {
            throw new IllegalArgumentException("commit_sha must be 7-64 lowercase hexadecimal characters");
        }
        return value;
    }

    private static String validateText(String fieldName, String value) {
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException(fieldName + " must be a non-empty string");
        }
        return value;
    }

    private static String derive(String namespace, Map<String, String> values) {
        Map<String, String> payload = new TreeMap<>(values);
        payload.put("namespace", namespace);
        StringBuilder canonical = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, String> entry : payload.entrySet()) {
            if (!first) {
                canonical.append(',');
            }
            canonical.append(jsonString(entry.getKey())).append(':').append(jsonString(entry.getValue()));
            first = false;
        }
        canonical.append('}');
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(canonical.toString().getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder();
            for (byte value : digest) {
                hex.append(String.format("%02x", value));
            }
            return "sha256:" + hex;
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }

    private static String jsonString(String value) {
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    private static String uuid() {
        return UUID.randomUUID().toString();
    }

    private static String stringValue(Map<String, ?> values, String name) {
        Object value = values.get(name);
        if (!(value instanceof String)) {
            throw new IllegalArgumentException(name + " must be a JSON string");
        }
        return (String) value;
    }
}
