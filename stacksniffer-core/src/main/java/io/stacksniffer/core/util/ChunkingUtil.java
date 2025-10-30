package io.stacksniffer.core.util;

import java.util.ArrayList;
import java.util.List;

public class ChunkingUtil {

    private static final int DEFAULT_CHUNK_SIZE = 512;
    private static final int DEFAULT_OVERLAP = 128;

    public static List<String> chunkByLines(String content, int chunkSize, int overlap) {
        List<String> chunks = new ArrayList<>();
        String[] lines = content.split("\n");

        if (lines.length == 0) {
            return chunks;
        }

        int start = 0;
        while (start < lines.length) {
            int end = Math.min(start + chunkSize, lines.length);
            StringBuilder chunk = new StringBuilder();

            for (int i = start; i < end; i++) {
                chunk.append(lines[i]);
                if (i < end - 1) {
                    chunk.append("\n");
                }
            }

            chunks.add(chunk.toString());
            start += (chunkSize - overlap);

            if (start >= lines.length) {
                break;
            }
        }

        return chunks;
    }

    public static List<String> chunkByTokens(String content, int maxTokens) {
        return chunkByTokens(content, maxTokens, 0);
    }

    public static List<String> chunkByTokens(String content, int maxTokens, int overlap) {
        List<String> chunks = new ArrayList<>();
        String[] words = content.split("\\s+");

        int start = 0;
        while (start < words.length) {
            int end = Math.min(start + maxTokens, words.length);
            StringBuilder chunk = new StringBuilder();

            for (int i = start; i < end; i++) {
                chunk.append(words[i]);
                if (i < end - 1) {
                    chunk.append(" ");
                }
            }

            chunks.add(chunk.toString());
            start += (maxTokens - overlap);

            if (start >= words.length) {
                break;
            }
        }

        return chunks;
    }

    public static List<String> chunkBySemanticBoundaries(String code, String language) {
        List<String> chunks = new ArrayList<>();

        if ("java".equalsIgnoreCase(language)) {
            chunks.addAll(extractJavaChunks(code));
        } else {
            chunks.addAll(chunkByLines(code, DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP));
        }

        return chunks;
    }

    private static List<String> extractJavaChunks(String code) {
        List<String> chunks = new ArrayList<>();
        String[] lines = code.split("\n");
        StringBuilder currentChunk = new StringBuilder();
        int braceCount = 0;
        boolean inMethod = false;

        for (String line : lines) {
            currentChunk.append(line).append("\n");

            for (char c : line.toCharArray()) {
                if (c == '{') {
                    braceCount++;
                    inMethod = true;
                } else if (c == '}') {
                    braceCount--;
                }
            }

            if (inMethod && braceCount == 0) {
                chunks.add(currentChunk.toString().trim());
                currentChunk = new StringBuilder();
                inMethod = false;
            }
        }

        if (currentChunk.length() > 0) {
            chunks.add(currentChunk.toString().trim());
        }

        return chunks;
    }

    public static int calculateOverlap(String chunk1, String chunk2) {
        if (chunk1 == null || chunk2 == null) {
            return 0;
        }

        int maxOverlap = Math.min(chunk1.length(), chunk2.length());

        for (int i = maxOverlap; i > 0; i--) {
            String end1 = chunk1.substring(chunk1.length() - i);
            String start2 = chunk2.substring(0, i);

            if (end1.equals(start2)) {
                return i;
            }
        }

        return 0;
    }
}
