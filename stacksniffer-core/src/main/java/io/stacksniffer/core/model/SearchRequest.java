package io.stacksniffer.core.model;

import java.util.List;

/**
 * Represents a search request for repositories
 */
public class SearchRequest {
    
    private String query;
    private List<String> technologies;
    private List<String> languages;
    private int limit;
    private int offset;

    public SearchRequest() {
        this.limit = 10;
        this.offset = 0;
    }

    public SearchRequest(String query, List<String> technologies, List<String> languages, int limit, int offset) {
        this.query = query;
        this.technologies = technologies;
        this.languages = languages;
        this.limit = limit;
        this.offset = offset;
    }

    public String getQuery() {
        return query;
    }

    public void setQuery(String query) {
        this.query = query;
    }

    public List<String> getTechnologies() {
        return technologies;
    }

    public void setTechnologies(List<String> technologies) {
        this.technologies = technologies;
    }

    public List<String> getLanguages() {
        return languages;
    }

    public void setLanguages(List<String> languages) {
        this.languages = languages;
    }

    public int getLimit() {
        return limit;
    }

    public void setLimit(int limit) {
        this.limit = limit;
    }

    public int getOffset() {
        return offset;
    }

    public void setOffset(int offset) {
        this.offset = offset;
    }
}
