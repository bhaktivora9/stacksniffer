package io.stacksniffer.ingestion.service;

import io.stacksniffer.core.model.RepoMetadata;

/**
 * Service for interacting with the GitHub API
 */
public class GitHubAPIService {
    
    /**
     * Fetches repository metadata from GitHub
     *
     * @param owner the repository owner
     * @param repoName the repository name
     * @return repository metadata
     */
    public RepoMetadata fetchRepository(String owner, String repoName) {
        // Implementation to be added
        return null;
    }

    /**
     * Fetches file contents from a GitHub repository
     *
     * @param owner the repository owner
     * @param repoName the repository name
     * @param path the file path
     * @return file contents
     */
    public String fetchFileContents(String owner, String repoName, String path) {
        // Implementation to be added
        return null;
    }
}
