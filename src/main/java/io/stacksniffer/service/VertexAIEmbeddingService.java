package io.stacksniffer.service;

import com.google.cloud.aiplatform.v1.*;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

@Service
@Slf4j
public class VertexAIEmbeddingService {

    @Value("${vertexai.project-id:}")
    private String projectId;

    @Value("${vertexai.location:us-central1}")
    private String location;

    @Value("${vertexai.embedding-model:textembedding-gecko@003}")
    private String embeddingModel;

    public List<Float> generateEmbedding(String text) throws IOException {
        if (projectId == null || projectId.isEmpty()) {
            log.warn("Vertex AI project ID not configured, returning dummy embedding");
            return generateDummyEmbedding();
        }

        try (PredictionServiceClient client = PredictionServiceClient.create()) {
            String endpoint = String.format("projects/%s/locations/%s/publishers/google/models/%s",
                projectId, location, embeddingModel);

            com.google.protobuf.Value.Builder instanceValue = com.google.protobuf.Value.newBuilder();
            instanceValue.getStructValueBuilder()
                .putFields("content", com.google.protobuf.Value.newBuilder().setStringValue(text).build());

            PredictRequest request = PredictRequest.newBuilder()
                .setEndpoint(endpoint)
                .addInstances(instanceValue.build())
                .build();

            PredictResponse response = client.predict(request);
            
            if (response.getPredictionsCount() > 0) {
                com.google.protobuf.Value prediction = response.getPredictions(0);
                List<Float> embedding = new ArrayList<>();
                
                com.google.protobuf.Value embeddingsValue = prediction.getStructValue()
                    .getFieldsOrDefault("embeddings", null);
                
                if (embeddingsValue != null) {
                    com.google.protobuf.Value valuesValue = embeddingsValue.getStructValue()
                        .getFieldsOrDefault("values", null);
                    
                    if (valuesValue != null) {
                        for (com.google.protobuf.Value val : valuesValue.getListValue().getValuesList()) {
                            embedding.add((float) val.getNumberValue());
                        }
                        return embedding;
                    }
                }
            }
        } catch (Exception e) {
            log.error("Error generating embedding with Vertex AI", e);
            return generateDummyEmbedding();
        }

        return generateDummyEmbedding();
    }

    private List<Float> generateDummyEmbedding() {
        List<Float> embedding = new ArrayList<>(768);
        for (int i = 0; i < 768; i++) {
            embedding.add((float) Math.random() * 0.1f);
        }
        return embedding;
    }
}
