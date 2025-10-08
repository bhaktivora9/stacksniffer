package io.stacksniffer.core.exception;

/**
 * Base exception class for all StackSniffer exceptions
 */
public class StackSnifferException extends RuntimeException {
    
    public StackSnifferException() {
        super();
    }

    public StackSnifferException(String message) {
        super(message);
    }

    public StackSnifferException(String message, Throwable cause) {
        super(message, cause);
    }

    public StackSnifferException(Throwable cause) {
        super(cause);
    }
}
