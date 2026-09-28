package com.fasterxml;

public final class Json {
    private Json() {}

    public static String quote(String value) {
        return "\"" + escape(value) + "\"";
    }

    static String escape(String value) {
        return value.replace("\"", "\\\"");
    }
}
