package io.stacksniffer.core.util;

import java.util.regex.Pattern;

public class TokenCounter {

    private static final Pattern WORD_PATTERN = Pattern.compile("\\w+");
    private static final Pattern WHITESPACE_PATTERN = Pattern.compile("\\s+");

    public static int countTokens(String text) {
        if (text == null || text.isEmpty()) {
            return 0;
        }

        return countTokensApproximate(text);
    }

    public static int countTokensApproximate(String text) {
        if (text == null || text.isEmpty()) {
            return 0;
        }

        String[] words = text.split("\\s+");
        int tokenCount = 0;

        for (String word : words) {
            if (word.length() <= 4) {
                tokenCount += 1;
            } else {
                tokenCount += (word.length() / 4) + 1;
            }
        }

        return tokenCount;
    }

    public static int countWords(String text) {
        if (text == null || text.isEmpty()) {
            return 0;
        }

        return text.split("\\s+").length;
    }

    public static int estimateTokensForCode(String code, String language) {
        if (code == null || code.isEmpty()) {
            return 0;
        }

        int baseTokens = countTokensApproximate(code);

        if ("java".equalsIgnoreCase(language)) {
            return (int) (baseTokens * 1.2);
        } else if ("python".equalsIgnoreCase(language)) {
            return (int) (baseTokens * 1.1);
        } else if ("javascript".equalsIgnoreCase(language) || "typescript".equalsIgnoreCase(language)) {
            return (int) (baseTokens * 1.15);
        }

        return baseTokens;
    }

    public static boolean exceedsTokenLimit(String text, int limit) {
        return countTokens(text) > limit;
    }

    public static String truncateToTokenLimit(String text, int limit) {
        if (text == null || text.isEmpty()) {
            return text;
        }

        int currentTokens = countTokens(text);
        if (currentTokens <= limit) {
            return text;
        }

        String[] words = text.split("\\s+");
        StringBuilder result = new StringBuilder();
        int tokenCount = 0;

        for (String word : words) {
            int wordTokens = (word.length() <= 4) ? 1 : (word.length() / 4) + 1;

            if (tokenCount + wordTokens > limit) {
                break;
            }

            result.append(word).append(" ");
            tokenCount += wordTokens;
        }

        return result.toString().trim();
    }
}
