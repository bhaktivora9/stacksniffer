package io.stacksniffer.core.model.domain;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class CodeChunk {
    private String id;
    private String repoName;
    private String filePath;
    private String language;
    private String chunkType;
    private String functionName;
    private String className;
    private String codeContent;
    private float[] embedding;
    private String embeddingModel;
    private LocalDateTime embeddingGeneratedAt;
    private List<TechStack> techStack;
    private DomainTags domainTags;
    private Map<String, Object> metadata;
    private int lineStart;
    private int lineEnd;
    private LocalDateTime indexedAt;
}
