package io.stacksniffer.core.util;

import java.nio.charset.StandardCharsets;
import java.util.Base64;

public class Base64Decoder {

    public static String decode(String base64Content) {
        if (base64Content == null || base64Content.isEmpty()) {
            return "";
        }

        try {
            byte[] decodedBytes = Base64.getDecoder().decode(base64Content);
            return new String(decodedBytes, StandardCharsets.UTF_8);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("Invalid base64 content", e);
        }
    }

    public static String encode(String content) {
        if (content == null || content.isEmpty()) {
            return "";
        }

        byte[] contentBytes = content.getBytes(StandardCharsets.UTF_8);
        return Base64.getEncoder().encodeToString(contentBytes);
    }

    public static boolean isBase64(String content) {
        if (content == null || content.isEmpty()) {
            return false;
        }

        try {
            Base64.getDecoder().decode(content);
            return true;
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    public static String decodeGitHubContent(String content) {
        if (content == null || content.isEmpty()) {
            return "";
        }

        String cleanedContent = content.replaceAll("\\s", "");

        return decode(cleanedContent);
    }
}
