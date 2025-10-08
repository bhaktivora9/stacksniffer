package io.stacksniffer.core.model;

import java.util.List;

/**
 * Represents the technology stack analysis results for a repository
 */
public class TechStackAnalysis {
    
    private String repositoryId;
    private List<String> technologies;
    private List<String> frameworks;
    private List<String> languages;
    private String primaryLanguage;
    private double confidenceScore;

    public TechStackAnalysis() {
    }

    public TechStackAnalysis(String repositoryId, List<String> technologies, List<String> frameworks, 
                           List<String> languages, String primaryLanguage, double confidenceScore) {
        this.repositoryId = repositoryId;
        this.technologies = technologies;
        this.frameworks = frameworks;
        this.languages = languages;
        this.primaryLanguage = primaryLanguage;
        this.confidenceScore = confidenceScore;
    }

    public String getRepositoryId() {
        return repositoryId;
    }

    public void setRepositoryId(String repositoryId) {
        this.repositoryId = repositoryId;
    }

    public List<String> getTechnologies() {
        return technologies;
    }

    public void setTechnologies(List<String> technologies) {
        this.technologies = technologies;
    }

    public List<String> getFrameworks() {
        return frameworks;
    }

    public void setFrameworks(List<String> frameworks) {
        this.frameworks = frameworks;
    }

    public List<String> getLanguages() {
        return languages;
    }

    public void setLanguages(List<String> languages) {
        this.languages = languages;
    }

    public String getPrimaryLanguage() {
        return primaryLanguage;
    }

    public void setPrimaryLanguage(String primaryLanguage) {
        this.primaryLanguage = primaryLanguage;
    }

    public double getConfidenceScore() {
        return confidenceScore;
    }

    public void setConfidenceScore(double confidenceScore) {
        this.confidenceScore = confidenceScore;
    }
}
