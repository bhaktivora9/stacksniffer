package io.stacksniffer.core.enums;

/**
 * Categories of technologies that can be detected in repositories
 */
public enum TechnologyCategory {
    
    LANGUAGE("Programming Language"),
    FRAMEWORK("Framework"),
    DATABASE("Database"),
    CLOUD_PLATFORM("Cloud Platform"),
    DEVOPS_TOOL("DevOps Tool"),
    LIBRARY("Library"),
    BUILD_TOOL("Build Tool"),
    TESTING_FRAMEWORK("Testing Framework"),
    CONTAINER("Container Technology"),
    MESSAGE_QUEUE("Message Queue"),
    WEB_SERVER("Web Server"),
    OTHER("Other");

    private final String displayName;

    TechnologyCategory(String displayName) {
        this.displayName = displayName;
    }

    public String getDisplayName() {
        return displayName;
    }
}
