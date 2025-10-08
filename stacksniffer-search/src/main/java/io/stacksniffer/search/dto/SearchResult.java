package io.stacksniffer.search.dto;

import io.stacksniffer.core.model.RepoMetadata;

/**
 * DTO representing a single search result
 */
public class SearchResult {
    
    private RepoMetadata repository;
    private double score;
    private double keywordScore;
    private double semanticScore;
    private String matchReason;

    public SearchResult() {
    }

    public SearchResult(RepoMetadata repository, double score, double keywordScore, 
                       double semanticScore, String matchReason) {
        this.repository = repository;
        this.score = score;
        this.keywordScore = keywordScore;
        this.semanticScore = semanticScore;
        this.matchReason = matchReason;
    }

    public RepoMetadata getRepository() {
        return repository;
    }

    public void setRepository(RepoMetadata repository) {
        this.repository = repository;
    }

    public double getScore() {
        return score;
    }

    public void setScore(double score) {
        this.score = score;
    }

    public double getKeywordScore() {
        return keywordScore;
    }

    public void setKeywordScore(double keywordScore) {
        this.keywordScore = keywordScore;
    }

    public double getSemanticScore() {
        return semanticScore;
    }

    public void setSemanticScore(double semanticScore) {
        this.semanticScore = semanticScore;
    }

    public String getMatchReason() {
        return matchReason;
    }

    public void setMatchReason(String matchReason) {
        this.matchReason = matchReason;
    }
}
