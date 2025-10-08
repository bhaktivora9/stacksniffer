package io.stacksniffer.core.exception;

/**
 * Exception thrown during repository ingestion and analysis
 */
public class IngestionException extends StackSnifferException {
    
    public IngestionException() {
        super();
    }

    public IngestionException(String message) {
        super(message);
    }

    public IngestionException(String message, Throwable cause) {
        super(message, cause);
    }

    public IngestionException(Throwable cause) {
        super(cause);
    }
}
