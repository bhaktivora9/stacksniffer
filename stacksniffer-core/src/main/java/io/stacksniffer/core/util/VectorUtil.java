package io.stacksniffer.core.util;

public class VectorUtil {

    public static double cosineSimilarity(float[] vectorA, float[] vectorB) {
        if (vectorA == null || vectorB == null) {
            throw new IllegalArgumentException("Vectors cannot be null");
        }

        if (vectorA.length != vectorB.length) {
            throw new IllegalArgumentException("Vectors must have the same length");
        }

        double dotProduct = 0.0;
        double normA = 0.0;
        double normB = 0.0;

        for (int i = 0; i < vectorA.length; i++) {
            dotProduct += vectorA[i] * vectorB[i];
            normA += vectorA[i] * vectorA[i];
            normB += vectorB[i] * vectorB[i];
        }

        normA = Math.sqrt(normA);
        normB = Math.sqrt(normB);

        if (normA == 0.0 || normB == 0.0) {
            return 0.0;
        }

        return dotProduct / (normA * normB);
    }

    public static float[] normalize(float[] vector) {
        if (vector == null) {
            throw new IllegalArgumentException("Vector cannot be null");
        }

        double norm = 0.0;
        for (float v : vector) {
            norm += v * v;
        }
        norm = Math.sqrt(norm);

        if (norm == 0.0) {
            return vector;
        }

        float[] normalized = new float[vector.length];
        for (int i = 0; i < vector.length; i++) {
            normalized[i] = (float) (vector[i] / norm);
        }

        return normalized;
    }

    public static double euclideanDistance(float[] vectorA, float[] vectorB) {
        if (vectorA == null || vectorB == null) {
            throw new IllegalArgumentException("Vectors cannot be null");
        }

        if (vectorA.length != vectorB.length) {
            throw new IllegalArgumentException("Vectors must have the same length");
        }

        double sum = 0.0;
        for (int i = 0; i < vectorA.length; i++) {
            double diff = vectorA[i] - vectorB[i];
            sum += diff * diff;
        }

        return Math.sqrt(sum);
    }

    public static double dotProduct(float[] vectorA, float[] vectorB) {
        if (vectorA == null || vectorB == null) {
            throw new IllegalArgumentException("Vectors cannot be null");
        }

        if (vectorA.length != vectorB.length) {
            throw new IllegalArgumentException("Vectors must have the same length");
        }

        double result = 0.0;
        for (int i = 0; i < vectorA.length; i++) {
            result += vectorA[i] * vectorB[i];
        }

        return result;
    }

    public static float[] add(float[] vectorA, float[] vectorB) {
        if (vectorA == null || vectorB == null) {
            throw new IllegalArgumentException("Vectors cannot be null");
        }

        if (vectorA.length != vectorB.length) {
            throw new IllegalArgumentException("Vectors must have the same length");
        }

        float[] result = new float[vectorA.length];
        for (int i = 0; i < vectorA.length; i++) {
            result[i] = vectorA[i] + vectorB[i];
        }

        return result;
    }

    public static float[] multiply(float[] vector, float scalar) {
        if (vector == null) {
            throw new IllegalArgumentException("Vector cannot be null");
        }

        float[] result = new float[vector.length];
        for (int i = 0; i < vector.length; i++) {
            result[i] = vector[i] * scalar;
        }

        return result;
    }
}
