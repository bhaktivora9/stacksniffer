package io.stacksniffer.core.model.config;

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
public class PatternConfig {
    private String id;
    private String name;
    private String description;
    private String version;
    private List<DetectionRule> rules;
    private List<DomainDefinition> domains;
    private Map<String, Object> settings;
    private boolean enabled;
    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;
    private String createdBy;
}
