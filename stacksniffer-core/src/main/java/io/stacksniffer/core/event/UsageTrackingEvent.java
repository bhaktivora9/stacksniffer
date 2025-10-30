package io.stacksniffer.core.event;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;
import java.util.Map;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class UsageTrackingEvent {
    private String eventId;
    private String userId;
    private String sessionId;
    private String eventType;
    private String action;
    private Map<String, Object> metadata;
    private LocalDateTime timestamp;
    private long durationMs;
}
