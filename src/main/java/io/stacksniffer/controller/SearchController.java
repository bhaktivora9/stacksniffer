package io.stacksniffer.controller;

import io.stacksniffer.model.CodeChunk;
import io.stacksniffer.model.SearchRequest;
import io.stacksniffer.model.SearchResponse;
import io.stacksniffer.service.ElasticsearchService;
import io.stacksniffer.service.VertexAIEmbeddingService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api")
@RequiredArgsConstructor
@Slf4j
public class SearchController {

    private final ElasticsearchService elasticsearchService;
    private final VertexAIEmbeddingService embeddingService;

    @PostMapping("/search")
    public ResponseEntity<SearchResponse> search(@RequestBody SearchRequest request) {
        try {
            long startTime = System.currentTimeMillis();
            
            log.info("Received search request: {}", request.getQuery());
            
            // Generate embedding for the query
            List<Float> queryEmbedding = null;
            if (request.getQuery() != null && !request.getQuery().isEmpty()) {
                queryEmbedding = embeddingService.generateEmbedding(request.getQuery());
            }
            
            // Perform hybrid search
            List<CodeChunk> results = elasticsearchService.hybridSearch(request, queryEmbedding);
            
            long searchTimeMs = System.currentTimeMillis() - startTime;
            
            SearchResponse response = SearchResponse.builder()
                .results(results)
                .totalResults(results.size())
                .searchTimeMs(searchTimeMs)
                .build();
            
            return ResponseEntity.ok(response);
            
        } catch (Exception e) {
            log.error("Error performing search", e);
            return ResponseEntity.internalServerError().build();
        }
    }
}
