package io.stacksniffer.core.model.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
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
public class IngestionRequest {
    @NotBlank(message = "Repository URL cannot be blank")
    private String repositoryUrl;

    @Pattern(regexp = "github|gitlab|bitbucket", message = "Provider must be github, gitlab, or bitbucket")
    @Builder.Default
    private String provider = "github";

    private String branch;
    private String accessToken;
    private List<String> includePatterns;
    private List<String> excludePatterns;

    @Builder.Default
    private boolean fullReindex = false;

    @Builder.Default
    private boolean generateEmbeddings = true;

    @Builder.Default
    private boolean detectTechStack = true;

    @Builder.Default
    private boolean analyzeDomains = true;

    private Map<String, Object> metadata;
}
