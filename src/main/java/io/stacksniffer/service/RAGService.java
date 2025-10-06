package io.stacksniffer.service;

import io.stacksniffer.model.CodeChunk;
import io.stacksniffer.model.SearchRequest;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.util.List;

@Service
@RequiredArgsConstructor
@Slf4j
public class RAGService {

    private final ElasticsearchService elasticsearchService;
    private final VertexAIEmbeddingService embeddingService;
    private final AgentService agentService;

    public String retrieveAndGenerate(String query, String repositoryUrl, String sessionId) {
        try {
            // Generate embedding for the query
            List<Float> queryEmbedding = embeddingService.generateEmbedding(query);
            
            // Search for relevant code chunks
            SearchRequest searchRequest = SearchRequest.builder()
                .query(query)
                .topK(5)
                .build();
            
            List<CodeChunk> relevantChunks = elasticsearchService.hybridSearch(searchRequest, queryEmbedding);
            
            // Build context from retrieved chunks
            StringBuilder context = new StringBuilder();
            for (CodeChunk chunk : relevantChunks) {
                context.append("File: ").append(chunk.getFilePath()).append("\n");
                context.append("Code:\n").append(chunk.getCode()).append("\n\n");
            }
            
            // Generate response using the agent
            return agentService.chat(query, sessionId, context.toString());
            
        } catch (IOException e) {
            log.error("Error in RAG pipeline", e);
            return "I encountered an error while processing your request.";
        }
    }

    public String retrieveContext(String query, String repositoryUrl) {
        try {
            List<Float> queryEmbedding = embeddingService.generateEmbedding(query);
            
            SearchRequest searchRequest = SearchRequest.builder()
                .query(query)
                .topK(3)
                .build();
            
            List<CodeChunk> relevantChunks = elasticsearchService.hybridSearch(searchRequest, queryEmbedding);
            
            StringBuilder context = new StringBuilder();
            for (CodeChunk chunk : relevantChunks) {
                context.append("File: ").append(chunk.getFilePath()).append("\n");
                context.append("Code: ").append(chunk.getCode().substring(0, Math.min(200, chunk.getCode().length())));
                context.append("...\n\n");
            }
            
            return context.toString();
            
        } catch (Exception e) {
            log.error("Error retrieving context", e);
            return "";
        }
    }
}
