package io.stacksniffer.model;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;
import java.util.Map;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class CodeChunk {
    private String id;
    private String repositoryUrl;
    private String filePath;
    private String code;
    private List<Float> vector;  // 768-dimensional embedding
    private List<String> tags;
    private Map<String, Object> metadata;
    private String language;
    private Integer startLine;
    private Integer endLine;
}
