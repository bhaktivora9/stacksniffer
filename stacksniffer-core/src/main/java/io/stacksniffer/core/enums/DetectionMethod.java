package io.stacksniffer.core.enums;

/**
 * Methods used to detect technologies in repositories
 */
public enum DetectionMethod {
    
    FILE_EXTENSION("File Extension Analysis"),
    DEPENDENCY_FILE("Dependency File Parsing"),
    CONFIG_FILE("Configuration File Analysis"),
    CODE_PATTERN("Code Pattern Matching"),
    AI_INFERENCE("AI-based Inference"),
    REPOSITORY_METADATA("Repository Metadata"),
    MANUAL("Manual Classification");

    private final String description;

    DetectionMethod(String description) {
        this.description = description;
    }

    public String getDescription() {
        return description;
    }
}
