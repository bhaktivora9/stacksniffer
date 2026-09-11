package io.stacksniffer.repository;

public record ClassificationRun(
        String classificationRunId,
        String analysisId,
        String classificationContractId,
        Status status) {
    public enum Status { QUEUED, RUNNING, COMPLETED, SUPERSEDED }

    public ClassificationRun {
        if (classificationRunId == null || classificationRunId.isBlank()
                || analysisId == null || analysisId.isBlank()) {
            throw new IllegalArgumentException("run and analysis IDs are required");
        }
        new ClassificationContractProjection(classificationContractId);
        if (status == null) {
            throw new IllegalArgumentException("status is required");
        }
    }

    public ClassificationRun supersede() {
        return new ClassificationRun(classificationRunId, analysisId, classificationContractId, Status.SUPERSEDED);
    }
}
