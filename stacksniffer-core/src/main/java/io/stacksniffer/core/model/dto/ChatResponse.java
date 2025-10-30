package io.stacksniffer.core.model.dto;

import io.stacksniffer.core.model.domain.CodeChunk;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;
import java.util.List;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class ChatResponse {
    private String sessionId;
    private String message;
    private String response;
    private List<CodeChunk> codeExamples;
    private List<String> references;
    private LocalDateTime timestamp;
    private long responseTimeMs;
    private String modelUsed;
    private int tokensUsed;
}
