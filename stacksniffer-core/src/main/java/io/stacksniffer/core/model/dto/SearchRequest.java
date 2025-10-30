package io.stacksniffer.core.model.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
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
public class SearchRequest {
	@NotBlank(message = "Query cannot be blank")
	private String query;

	private List<String> languages;
	private List<String> repositories;
	private List<String> techStack;
	private String repositoryName;
	private List<String> domains;
	private String chunkType;

	@Min(value = 1, message = "Page size must be at least 1")
	@Max(value = 100, message = "Page size cannot exceed 100")
	@Builder.Default
	private int pageSize = 10;

	@Min(value = 0, message = "Page number must be non-negative")
	@Builder.Default
	private int page = 0;

	@Builder.Default
	private boolean includeEmbeddings = false;

	@Builder.Default
	private boolean useSemanticSearch = true;

	private Map<String, Object> filters;
}
