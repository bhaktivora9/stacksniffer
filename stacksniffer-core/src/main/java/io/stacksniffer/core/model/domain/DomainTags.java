package io.stacksniffer.core.model.domain;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class DomainTags {
    private String primaryDomain;
    private List<String> secondaryDomains;
    private List<String> tags;
    private int complexityScore;
}
