package io.stacksniffer.core.model.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class ChatRequest {
	@NotBlank(message = "Message cannot be blank")
	private String message;

	private String sessionId;
	private List<String> contextRepositories;
	private List<String> contextLanguages;

	@Builder.Default
	private boolean includeCodeExamples = true;

	@Builder.Default
	private int maxCodeExamples = 3;

	@Builder.Default
	private boolean streamResponse = false;

	private String userId;
	private String conversationId;

}
