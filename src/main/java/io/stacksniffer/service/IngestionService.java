package io.stacksniffer.service;

import io.stacksniffer.model.CodeChunk;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

@Service
@RequiredArgsConstructor
@Slf4j
public class IngestionService {

    private final GitHubAPIService gitHubAPIService;
    private final CodeParserService codeParserService;
    private final VertexAIEmbeddingService embeddingService;
    private final DomainTaggerService domainTaggerService;
    private final ElasticsearchService elasticsearchService;

    public void ingestRepository(String repositoryUrl, String branch, Boolean includeTests) throws IOException {
        log.info("Starting ingestion for repository: {}", repositoryUrl);
        
        // Fetch repository files
        Map<String, String> files = gitHubAPIService.fetchRepositoryFiles(repositoryUrl, branch);
        log.info("Fetched {} files from repository", files.size());
        
        List<CodeChunk> allChunks = new ArrayList<>();
        
        // Process each file
        for (Map.Entry<String, String> entry : files.entrySet()) {
            String filePath = entry.getKey();
            String content = entry.getValue();
            
            // Skip test files if requested
            if (Boolean.FALSE.equals(includeTests) && isTestFile(filePath)) {
                continue;
            }
            
            // Parse code into chunks
            List<CodeChunk> chunks = codeParserService.parseCodeFile(filePath, content, repositoryUrl);
            
            // Process each chunk
            for (CodeChunk chunk : chunks) {
                try {
                    // Generate embedding
                    List<Float> embedding = embeddingService.generateEmbedding(chunk.getCode());
                    chunk.setVector(embedding);
                    
                    // Add domain tags
                    List<String> domainTags = domainTaggerService.generateDomainTags(chunk.getCode());
                    if (chunk.getTags() == null) {
                        chunk.setTags(domainTags);
                    } else {
                        chunk.getTags().addAll(domainTags);
                    }
                    
                    allChunks.add(chunk);
                    
                } catch (Exception e) {
                    log.error("Error processing chunk from {}", filePath, e);
                }
            }
        }
        
        // Bulk index all chunks
        if (!allChunks.isEmpty()) {
            elasticsearchService.bulkIndexCodeChunks(allChunks);
            log.info("Successfully ingested {} code chunks from {}", allChunks.size(), repositoryUrl);
        } else {
            log.warn("No code chunks were created for repository {}", repositoryUrl);
        }
    }

    private boolean isTestFile(String filePath) {
        String lowerPath = filePath.toLowerCase();
        return lowerPath.contains("/test/") || 
               lowerPath.contains("/tests/") || 
               lowerPath.endsWith("test.java") ||
               lowerPath.endsWith("test.py") ||
               lowerPath.endsWith("test.js") ||
               lowerPath.endsWith("spec.js") ||
               lowerPath.endsWith("spec.ts");
    }
}
