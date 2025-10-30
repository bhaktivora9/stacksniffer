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
public class DomainDefinition {
    private String id;
    private String name;
    private String description;
    private List<String> keywords;
    private List<String> patterns;
    private List<String> relatedDomains;
    private String category;
    private int complexityWeight;
    private Map<String, List<String>> contextualIndicators;
    private boolean enabled;
}
