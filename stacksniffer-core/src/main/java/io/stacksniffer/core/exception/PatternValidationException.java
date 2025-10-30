package io.stacksniffer.core.exception;

import java.util.List;

public class PatternValidationException extends RuntimeException {
    private final String patternId;
    private final List<String> validationErrors;

    public PatternValidationException(String message) {
        super(message);
        this.patternId = null;
        this.validationErrors = null;
    }

    public PatternValidationException(String message, Throwable cause) {
        super(message, cause);
        this.patternId = null;
        this.validationErrors = null;
    }

    public PatternValidationException(String message, String patternId, List<String> validationErrors) {
        super(message);
        this.patternId = patternId;
        this.validationErrors = validationErrors;
    }

    public String getPatternId() {
        return patternId;
    }

    public List<String> getValidationErrors() {
        return validationErrors;
    }
}
