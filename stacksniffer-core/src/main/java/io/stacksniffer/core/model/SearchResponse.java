package io.stacksniffer.core.model;

import java.util.List;

/**
 * Represents the response for a search request
 */
public class SearchResponse {
    
    private List<RepoMetadata> repositories;
    private int totalCount;
    private int page;
    private int pageSize;

    public SearchResponse() {
    }

    public SearchResponse(List<RepoMetadata> repositories, int totalCount, int page, int pageSize) {
        this.repositories = repositories;
        this.totalCount = totalCount;
        this.page = page;
        this.pageSize = pageSize;
    }

    public List<RepoMetadata> getRepositories() {
        return repositories;
    }

    public void setRepositories(List<RepoMetadata> repositories) {
        this.repositories = repositories;
    }

    public int getTotalCount() {
        return totalCount;
    }

    public void setTotalCount(int totalCount) {
        this.totalCount = totalCount;
    }

    public int getPage() {
        return page;
    }

    public void setPage(int page) {
        this.page = page;
    }

    public int getPageSize() {
        return pageSize;
    }

    public void setPageSize(int pageSize) {
        this.pageSize = pageSize;
    }
}
