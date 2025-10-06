package io.stacksniffer.service;

import io.stacksniffer.model.CodeChunk;
import io.stacksniffer.model.TechStackAnalysis;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.util.*;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
@Slf4j
public class TechAnalyzerService {

    private final ElasticsearchService elasticsearchService;
    private final DomainTaggerService domainTaggerService;
    private final GitHubAPIService gitHubAPIService;

    public TechStackAnalysis analyzeRepository(String repositoryUrl) throws IOException {
        // Fetch code chunks from Elasticsearch
        List<CodeChunk> codeChunks = elasticsearchService.searchByRepository(repositoryUrl);
        
        // Get repository metadata
        Map<String, Object> metadata = gitHubAPIService.getRepositoryMetadata(repositoryUrl);
        
        // Analyze languages
        Map<String, Long> languageCounts = codeChunks.stream()
            .filter(c -> c.getLanguage() != null)
            .collect(Collectors.groupingBy(CodeChunk::getLanguage, Collectors.counting()));
        
        List<String> languages = languageCounts.entrySet().stream()
            .sorted(Map.Entry.<String, Long>comparingByValue().reversed())
            .map(Map.Entry::getKey)
            .collect(Collectors.toList());
        
        // Detect frameworks and libraries
        Set<String> frameworks = new HashSet<>();
        Set<String> libraries = new HashSet<>();
        Set<String> tools = new HashSet<>();
        
        for (CodeChunk chunk : codeChunks) {
            String code = chunk.getCode().toLowerCase();
            
            // Java frameworks
            if (code.contains("@springboot") || code.contains("spring.boot")) frameworks.add("Spring Boot");
            if (code.contains("@restcontroller") || code.contains("spring.web")) frameworks.add("Spring Web");
            if (code.contains("hibernate")) frameworks.add("Hibernate");
            
            // JavaScript frameworks
            if (code.contains("react")) frameworks.add("React");
            if (code.contains("angular")) frameworks.add("Angular");
            if (code.contains("vue")) frameworks.add("Vue.js");
            if (code.contains("express")) frameworks.add("Express.js");
            
            // Python frameworks
            if (code.contains("django")) frameworks.add("Django");
            if (code.contains("flask")) frameworks.add("Flask");
            if (code.contains("fastapi")) frameworks.add("FastAPI");
            
            // Libraries
            if (code.contains("lombok")) libraries.add("Lombok");
            if (code.contains("jackson")) libraries.add("Jackson");
            if (code.contains("gson")) libraries.add("Gson");
            if (code.contains("pandas")) libraries.add("Pandas");
            if (code.contains("numpy")) libraries.add("NumPy");
            if (code.contains("tensorflow")) libraries.add("TensorFlow");
            if (code.contains("pytorch")) libraries.add("PyTorch");
            
            // Tools
            if (code.contains("docker")) tools.add("Docker");
            if (code.contains("kubernetes")) tools.add("Kubernetes");
            if (code.contains("maven")) tools.add("Maven");
            if (code.contains("gradle")) tools.add("Gradle");
            if (code.contains("npm")) tools.add("npm");
        }
        
        // Analyze domain
        String allCode = codeChunks.stream()
            .map(CodeChunk::getCode)
            .collect(Collectors.joining("\n"));
        
        Map<String, String> domainScores = domainTaggerService.tagDomain(allCode);
        String primaryDomain = domainTaggerService.determinePrimaryDomain(domainScores);
        
        // Calculate confidence score
        double confidence = calculateConfidence(codeChunks.size(), frameworks.size(), libraries.size());
        
        // Generate summary
        String summary = generateSummary(repositoryUrl, languages, frameworks, primaryDomain);
        
        return TechStackAnalysis.builder()
            .repositoryUrl(repositoryUrl)
            .languages(languages)
            .frameworks(new ArrayList<>(frameworks))
            .libraries(new ArrayList<>(libraries))
            .tools(new ArrayList<>(tools))
            .domainTags(domainScores)
            .primaryDomain(primaryDomain)
            .confidenceScore(confidence)
            .summary(summary)
            .build();
    }

    private double calculateConfidence(int chunkCount, int frameworkCount, int libraryCount) {
        double baseScore = Math.min(chunkCount / 100.0, 1.0) * 0.4;
        double techScore = Math.min((frameworkCount + libraryCount) / 10.0, 1.0) * 0.6;
        return Math.min(baseScore + techScore, 1.0);
    }

    private String generateSummary(String repoUrl, List<String> languages, 
                                   Set<String> frameworks, String domain) {
        StringBuilder summary = new StringBuilder();
        
        summary.append("This repository ");
        
        if (!languages.isEmpty()) {
            summary.append("primarily uses ")
                .append(String.join(", ", languages.subList(0, Math.min(3, languages.size()))))
                .append(". ");
        }
        
        if (!frameworks.isEmpty()) {
            summary.append("Key frameworks include ")
                .append(String.join(", ", new ArrayList<>(frameworks).subList(0, Math.min(3, frameworks.size()))))
                .append(". ");
        }
        
        summary.append("The codebase appears to be in the ")
            .append(domain)
            .append(" domain.");
        
        return summary.toString();
    }
}
