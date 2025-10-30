package io.stacksniffer.core.model.dto;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class MonitoringStats {
    private Long totalPatterns;
    private Long activePatterns;
    private Long pendingReview;
    private Long autoApproved;
    private Long approved;
    private Long rejected;
    private Double averageConfidence;
    private Long totalRepositories;
    private Long totalCodeChunks;
}
