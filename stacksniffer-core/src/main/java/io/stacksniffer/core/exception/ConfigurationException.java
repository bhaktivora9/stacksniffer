package io.stacksniffer.core.exception;

public class ConfigurationException extends RuntimeException {
    private final String configKey;
    private final String configValue;

    public ConfigurationException(String message) {
        super(message);
        this.configKey = null;
        this.configValue = null;
    }

    public ConfigurationException(String message, Throwable cause) {
        super(message, cause);
        this.configKey = null;
        this.configValue = null;
    }

    public ConfigurationException(String message, String configKey) {
        super(message);
        this.configKey = configKey;
        this.configValue = null;
    }

    public ConfigurationException(String message, String configKey, String configValue) {
        super(message);
        this.configKey = configKey;
        this.configValue = configValue;
    }

    public ConfigurationException(String message, String configKey, Throwable cause) {
        super(message, cause);
        this.configKey = configKey;
        this.configValue = null;
    }

    public String getConfigKey() {
        return configKey;
    }

    public String getConfigValue() {
        return configValue;
    }
}
