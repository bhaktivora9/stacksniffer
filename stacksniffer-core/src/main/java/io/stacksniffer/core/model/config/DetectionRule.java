package io.stacksniffer.core.model.config;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;
import java.util.Map;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class DetectionRule {
    private String id;
    private String name;
    private String description;
    private String ruleType;
    private List<String> patterns;
    private List<String> filePatterns;
    private List<String> languages;
    private String category;
    private double confidenceScore;
    private int priority;
    private Map<String, Object> conditions;
    private boolean enabled;
    private List<String> tags;
}
