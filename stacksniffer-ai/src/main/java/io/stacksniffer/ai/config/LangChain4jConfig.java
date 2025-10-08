package io.stacksniffer.ai.config;

/**
 * Configuration class for LangChain4j integration
 */
public class LangChain4jConfig {
    
    private String embeddingModel;
    private String chatModel;
    private int embeddingDimension;
    private boolean enableMemory;
    private int maxMemoryMessages;

    public LangChain4jConfig() {
        this.embeddingDimension = 768;
        this.enableMemory = true;
        this.maxMemoryMessages = 10;
    }

    public String getEmbeddingModel() {
        return embeddingModel;
    }

    public void setEmbeddingModel(String embeddingModel) {
        this.embeddingModel = embeddingModel;
    }

    public String getChatModel() {
        return chatModel;
    }

    public void setChatModel(String chatModel) {
        this.chatModel = chatModel;
    }

    public int getEmbeddingDimension() {
        return embeddingDimension;
    }

    public void setEmbeddingDimension(int embeddingDimension) {
        this.embeddingDimension = embeddingDimension;
    }

    public boolean isEnableMemory() {
        return enableMemory;
    }

    public void setEnableMemory(boolean enableMemory) {
        this.enableMemory = enableMemory;
    }

    public int getMaxMemoryMessages() {
        return maxMemoryMessages;
    }

    public void setMaxMemoryMessages(int maxMemoryMessages) {
        this.maxMemoryMessages = maxMemoryMessages;
    }
}
