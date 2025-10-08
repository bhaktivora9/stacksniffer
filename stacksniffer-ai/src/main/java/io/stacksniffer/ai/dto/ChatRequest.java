package io.stacksniffer.ai.dto;

import java.util.List;

/**
 * Request DTO for chat interactions
 */
public class ChatRequest {
    
    private String message;
    private String conversationId;
    private List<String> context;
    private boolean useRAG;
    private String repositoryId;

    public ChatRequest() {
        this.useRAG = false;
    }

    public ChatRequest(String message, String conversationId, List<String> context, 
                      boolean useRAG, String repositoryId) {
        this.message = message;
        this.conversationId = conversationId;
        this.context = context;
        this.useRAG = useRAG;
        this.repositoryId = repositoryId;
    }

    public String getMessage() {
        return message;
    }

    public void setMessage(String message) {
        this.message = message;
    }

    public String getConversationId() {
        return conversationId;
    }

    public void setConversationId(String conversationId) {
        this.conversationId = conversationId;
    }

    public List<String> getContext() {
        return context;
    }

    public void setContext(List<String> context) {
        this.context = context;
    }

    public boolean isUseRAG() {
        return useRAG;
    }

    public void setUseRAG(boolean useRAG) {
        this.useRAG = useRAG;
    }

    public String getRepositoryId() {
        return repositoryId;
    }

    public void setRepositoryId(String repositoryId) {
        this.repositoryId = repositoryId;
    }
}
