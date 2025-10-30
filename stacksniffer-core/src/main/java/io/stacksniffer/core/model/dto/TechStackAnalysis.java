package io.stacksniffer.core.model.dto;

import io.stacksniffer.core.model.domain.TechStack;
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
public class TechStackAnalysis {
    private String repositoryUrl;
    private String repositoryName;
    private List<TechStack> detectedTechnologies;
    private Map<String, Integer> languageDistribution;
    private List<String> frameworks;
    private List<String> libraries;
    private List<String> databases;
    private List<String> tools;
    private String primaryLanguage;
    private String architecturePattern;
    private double overallConfidence;
}
