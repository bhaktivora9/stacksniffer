package io.stacksniffer.core.model.domain;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class TechStack {
    private String name;
    private String version;
    private double confidence;
    private String category;
    private String detectionMethod;
}
