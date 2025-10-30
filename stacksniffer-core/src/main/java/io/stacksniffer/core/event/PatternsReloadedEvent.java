package io.stacksniffer.core.event;

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
public class PatternsReloadedEvent {
    private String eventId;
    private int totalPatterns;
    private int enabledPatterns;
    private List<String> configIds;
    private LocalDateTime timestamp;
    private String triggeredBy;
    private boolean success;
    private String errorMessage;
}
