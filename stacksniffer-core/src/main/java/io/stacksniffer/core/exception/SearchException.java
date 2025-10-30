package io.stacksniffer.core.exception;

public class SearchException extends RuntimeException {
    private final String query;
    private final String errorCode;

    public SearchException(String message) {
        super(message);
        this.query = null;
        this.errorCode = null;
    }

    public SearchException(String message, Throwable cause) {
        super(message, cause);
        this.query = null;
        this.errorCode = null;
    }

    public SearchException(String message, String query) {
        super(message);
        this.query = query;
        this.errorCode = null;
    }

    public SearchException(String message, String query, String errorCode) {
        super(message);
        this.query = query;
        this.errorCode = errorCode;
    }

    public SearchException(String message, String query, String errorCode, Throwable cause) {
        super(message, cause);
        this.query = query;
        this.errorCode = errorCode;
    }

    public String getQuery() {
        return query;
    }

    public String getErrorCode() {
        return errorCode;
    }
}
