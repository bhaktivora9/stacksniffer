package io.stacksniffer.core.model.domain;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.Map;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class SearchResult {
	private CodeChunk codeChunk;
	private String query;
	private String result;
	private String source;
	private double score;
	private double relevance;
	private Map<String, Object> highlights;
	private String explanation;
}
