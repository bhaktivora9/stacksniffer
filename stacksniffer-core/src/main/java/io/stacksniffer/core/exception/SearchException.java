package io.stacksniffer.core.exception;

/**
 * Exception thrown during search operations
 */
public class SearchException extends StackSnifferException {
    
    public SearchException() {
        super();
    }

    public SearchException(String message) {
        super(message);
    }

    public SearchException(String message, Throwable cause) {
        super(message, cause);
    }

    public SearchException(Throwable cause) {
        super(cause);
    }
}
