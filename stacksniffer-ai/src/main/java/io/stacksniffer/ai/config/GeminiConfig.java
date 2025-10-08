package io.stacksniffer.ai.config;

/**
 * Configuration class for Google Gemini AI
 */
public class GeminiConfig {
    
    private String apiKey;
    private String modelName;
    private double temperature;
    private int maxTokens;
    private String apiEndpoint;

    public GeminiConfig() {
        this.modelName = "gemini-pro";
        this.temperature = 0.7;
        this.maxTokens = 2048;
    }

    public String getApiKey() {
        return apiKey;
    }

    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    public String getModelName() {
        return modelName;
    }

    public void setModelName(String modelName) {
        this.modelName = modelName;
    }

    public double getTemperature() {
        return temperature;
    }

    public void setTemperature(double temperature) {
        this.temperature = temperature;
    }

    public int getMaxTokens() {
        return maxTokens;
    }

    public void setMaxTokens(int maxTokens) {
        this.maxTokens = maxTokens;
    }

    public String getApiEndpoint() {
        return apiEndpoint;
    }

    public void setApiEndpoint(String apiEndpoint) {
        this.apiEndpoint = apiEndpoint;
    }
}
