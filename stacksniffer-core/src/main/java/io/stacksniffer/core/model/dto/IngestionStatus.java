package io.stacksniffer.core.model.dto;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;
import java.util.List;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class IngestionStatus {
    private String jobId;
    private String repositoryUrl;
    private String status;
    private int totalFiles;
    private int processedFiles;
    private int failedFiles;
    private int totalChunks;
    private int indexedChunks;
    private double progressPercentage;
    private LocalDateTime startedAt;
    private LocalDateTime completedAt;
    private Long durationMs;
    private List<String> errors;
    private String currentFile;
    private String phase;
}
