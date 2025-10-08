package io.stacksniffer.ai.dto;

import java.util.List;

/**
 * Response DTO for chat interactions
 */
public class ChatResponse {
    
    private String response;
    private String conversationId;
    private List<String> sources;
    private double confidence;
    private long processingTimeMs;

    public ChatResponse() {
    }

    public ChatResponse(String response, String conversationId, List<String> sources, 
                       double confidence, long processingTimeMs) {
        this.response = response;
        this.conversationId = conversationId;
        this.sources = sources;
        this.confidence = confidence;
        this.processingTimeMs = processingTimeMs;
    }

    public String getResponse() {
        return response;
    }

    public void setResponse(String response) {
        this.response = response;
    }

    public String getConversationId() {
        return conversationId;
    }

    public void setConversationId(String conversationId) {
        this.conversationId = conversationId;
    }

    public List<String> getSources() {
        return sources;
    }

    public void setSources(List<String> sources) {
        this.sources = sources;
    }

    public double getConfidence() {
        return confidence;
    }

    public void setConfidence(double confidence) {
        this.confidence = confidence;
    }

    public long getProcessingTimeMs() {
        return processingTimeMs;
    }

    public void setProcessingTimeMs(long processingTimeMs) {
        this.processingTimeMs = processingTimeMs;
    }
}
