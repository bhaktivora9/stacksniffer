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
public class IngestionProgressEvent {
    private String eventId;
    private String jobId;
    private String repositoryUrl;
    private String currentFile;
    private int processedFiles;
    private int totalFiles;
    private double progressPercentage;
    private String phase;
    private LocalDateTime timestamp;
}
