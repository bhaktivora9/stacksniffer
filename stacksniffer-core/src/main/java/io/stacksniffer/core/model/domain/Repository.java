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
public class Repository {
    private String id;
    private String name;
    private String owner;
    private String url;
    private String defaultBranch;
    private String description;
    private List<String> languages;
    private List<TechStack> techStack;
    private Map<String, Object> metadata;
    private LocalDateTime lastIndexedAt;
    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;
    private int totalFiles;
    private int totalChunks;
    private String status;
}
