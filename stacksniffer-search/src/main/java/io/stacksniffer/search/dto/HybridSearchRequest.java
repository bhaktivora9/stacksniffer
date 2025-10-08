package io.stacksniffer.search.dto;

import io.stacksniffer.core.model.SearchRequest;
import java.util.List;

/**
 * Request DTO for hybrid search combining keyword and semantic search
 */
public class HybridSearchRequest extends SearchRequest {
    
    private List<Float> queryEmbedding;
    private double keywordWeight;
    private double semanticWeight;
    private boolean useReranking;

    public HybridSearchRequest() {
        super();
        this.keywordWeight = 0.5;
        this.semanticWeight = 0.5;
        this.useReranking = false;
    }

    public List<Float> getQueryEmbedding() {
        return queryEmbedding;
    }

    public void setQueryEmbedding(List<Float> queryEmbedding) {
        this.queryEmbedding = queryEmbedding;
    }

    public double getKeywordWeight() {
        return keywordWeight;
    }

    public void setKeywordWeight(double keywordWeight) {
        this.keywordWeight = keywordWeight;
    }

    public double getSemanticWeight() {
        return semanticWeight;
    }

    public void setSemanticWeight(double semanticWeight) {
        this.semanticWeight = semanticWeight;
    }

    public boolean isUseReranking() {
        return useReranking;
    }

    public void setUseReranking(boolean useReranking) {
        this.useReranking = useReranking;
    }
}
