package io.stacksniffer.ingestion.service;

import io.stacksniffer.core.model.RepoMetadata;

/**
 * Service for extracting metadata from GitHub repositories
 */
public class GitHubMetadataExtractor {
    
    /**
     * Extracts metadata from a GitHub repository
     *
     * @param owner the repository owner
     * @param repoName the repository name
     * @return extracted repository metadata
     */
    public RepoMetadata extractMetadata(String owner, String repoName) {
        // Implementation to be added
        return null;
    }

    /**
     * Extracts language statistics from a repository
     *
     * @param owner the repository owner
     * @param repoName the repository name
     * @return language statistics
     */
    public java.util.Map<String, Integer> extractLanguageStats(String owner, String repoName) {
        // Implementation to be added
        return null;
    }
}
