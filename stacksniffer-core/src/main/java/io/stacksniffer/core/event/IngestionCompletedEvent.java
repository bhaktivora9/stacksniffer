package io.stacksniffer.core.event;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class IngestionCompletedEvent {
    private String eventId;
    private String jobId;
    private String repositoryUrl;
    private int totalFiles;
    private int processedFiles;
    private int failedFiles;
    private int totalChunks;
    private long durationMs;
    private LocalDateTime completedAt;
    private boolean success;
    private String errorMessage;
}
