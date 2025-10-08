package io.stacksniffer.ingestion.dto;

/**
 * Request DTO for repository ingestion
 */
public class IngestRequest {
    
    private String owner;
    private String repositoryName;
    private String branch;
    private boolean analyzeFullHistory;

    public IngestRequest() {
        this.branch = "main";
        this.analyzeFullHistory = false;
    }

    public IngestRequest(String owner, String repositoryName, String branch, boolean analyzeFullHistory) {
        this.owner = owner;
        this.repositoryName = repositoryName;
        this.branch = branch;
        this.analyzeFullHistory = analyzeFullHistory;
    }

    public String getOwner() {
        return owner;
    }

    public void setOwner(String owner) {
        this.owner = owner;
    }

    public String getRepositoryName() {
        return repositoryName;
    }

    public void setRepositoryName(String repositoryName) {
        this.repositoryName = repositoryName;
    }

    public String getBranch() {
        return branch;
    }

    public void setBranch(String branch) {
        this.branch = branch;
    }

    public boolean isAnalyzeFullHistory() {
        return analyzeFullHistory;
    }

    public void setAnalyzeFullHistory(boolean analyzeFullHistory) {
        this.analyzeFullHistory = analyzeFullHistory;
    }
}
