package io.stacksniffer.ingestion.dto;

import io.stacksniffer.core.model.TechStackAnalysis;

/**
 * Response DTO for repository ingestion
 */
public class IngestResponse {
    
    private String repositoryId;
    private String status;
    private TechStackAnalysis analysis;
    private String message;
    private long processingTimeMs;

    public IngestResponse() {
    }

    public IngestResponse(String repositoryId, String status, TechStackAnalysis analysis, 
                         String message, long processingTimeMs) {
        this.repositoryId = repositoryId;
        this.status = status;
        this.analysis = analysis;
        this.message = message;
        this.processingTimeMs = processingTimeMs;
    }

    public String getRepositoryId() {
        return repositoryId;
    }

    public void setRepositoryId(String repositoryId) {
        this.repositoryId = repositoryId;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public TechStackAnalysis getAnalysis() {
        return analysis;
    }

    public void setAnalysis(TechStackAnalysis analysis) {
        this.analysis = analysis;
    }

    public String getMessage() {
        return message;
    }

    public void setMessage(String message) {
        this.message = message;
    }

    public long getProcessingTimeMs() {
        return processingTimeMs;
    }

    public void setProcessingTimeMs(long processingTimeMs) {
        this.processingTimeMs = processingTimeMs;
    }
}
