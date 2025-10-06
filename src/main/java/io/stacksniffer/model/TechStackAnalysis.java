package io.stacksniffer.model;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;
import java.util.Map;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class TechStackAnalysis {
    private String repositoryUrl;
    private List<String> languages;
    private List<String> frameworks;
    private List<String> libraries;
    private List<String> tools;
    private Map<String, String> domainTags;
    private String primaryDomain;
    private Double confidenceScore;
    private String summary;
}
