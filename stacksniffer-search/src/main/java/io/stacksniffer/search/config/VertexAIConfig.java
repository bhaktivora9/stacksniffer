package io.stacksniffer.search.config;

/**
 * Configuration class for Vertex AI
 */
public class VertexAIConfig {
    
    private String projectId;
    private String location;
    private String modelName;
    private String apiEndpoint;

    public VertexAIConfig() {
        this.location = "us-central1";
        this.modelName = "textembedding-gecko@003";
    }

    public String getProjectId() {
        return projectId;
    }

    public void setProjectId(String projectId) {
        this.projectId = projectId;
    }

    public String getLocation() {
        return location;
    }

    public void setLocation(String location) {
        this.location = location;
    }

    public String getModelName() {
        return modelName;
    }

    public void setModelName(String modelName) {
        this.modelName = modelName;
    }

    public String getApiEndpoint() {
        return apiEndpoint;
    }

    public void setApiEndpoint(String apiEndpoint) {
        this.apiEndpoint = apiEndpoint;
    }
}
