package io.stacksniffer.model;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class IngestRequest {
    private String repositoryUrl;
    private String branch;
    private Boolean includeTests;
}
