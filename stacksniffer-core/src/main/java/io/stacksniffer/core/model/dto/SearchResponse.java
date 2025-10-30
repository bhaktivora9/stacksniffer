package io.stacksniffer.core.model.dto;

import io.stacksniffer.core.model.domain.SearchResult;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;
import java.util.List;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class SearchResponse {
    private List<SearchResult> results;
    private long totalResults;
    private int page;
    private int pageSize;
    private int totalPages;
    private long searchTimeMs;
    private LocalDateTime timestamp;
    private String query;
    private boolean semanticSearchUsed;
}
