package io.stacksniffer.ingestion.config;

/**
 * Configuration class for GitHub API integration
 */
public class GitHubConfig {
    
    private String apiToken;
    private String apiBaseUrl;
    private int rateLimit;
    private int timeout;

    public GitHubConfig() {
        this.apiBaseUrl = "https://api.github.com";
        this.rateLimit = 5000;
        this.timeout = 30000;
    }

    public String getApiToken() {
        return apiToken;
    }

    public void setApiToken(String apiToken) {
        this.apiToken = apiToken;
    }

    public String getApiBaseUrl() {
        return apiBaseUrl;
    }

    public void setApiBaseUrl(String apiBaseUrl) {
        this.apiBaseUrl = apiBaseUrl;
    }

    public int getRateLimit() {
        return rateLimit;
    }

    public void setRateLimit(int rateLimit) {
        this.rateLimit = rateLimit;
    }

    public int getTimeout() {
        return timeout;
    }

    public void setTimeout(int timeout) {
        this.timeout = timeout;
    }
}
