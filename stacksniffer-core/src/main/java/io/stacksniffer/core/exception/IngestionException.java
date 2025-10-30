package io.stacksniffer.core.exception;

public class IngestionException extends RuntimeException {
    private final String jobId;
    private final String repositoryUrl;

    public IngestionException(String message) {
        super(message);
        this.jobId = null;
        this.repositoryUrl = null;
    }

    public IngestionException(String message, Throwable cause) {
        super(message, cause);
        this.jobId = null;
        this.repositoryUrl = null;
    }

    public IngestionException(String message, String jobId, String repositoryUrl) {
        super(message);
        this.jobId = jobId;
        this.repositoryUrl = repositoryUrl;
    }

    public IngestionException(String message, String jobId, String repositoryUrl, Throwable cause) {
        super(message, cause);
        this.jobId = jobId;
        this.repositoryUrl = repositoryUrl;
    }

    public String getJobId() {
        return jobId;
    }

    public String getRepositoryUrl() {
        return repositoryUrl;
    }
}
