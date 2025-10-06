package io.stacksniffer.service;

import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.vertexai.VertexAiGeminiChatModel;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import jakarta.annotation.PostConstruct;
import java.util.HashMap;
import java.util.Map;

@Service
@Slf4j
public class AgentService {

    @Value("${vertexai.project-id:}")
    private String projectId;

    @Value("${vertexai.location:us-central1}")
    private String location;

    @Value("${vertexai.chat-model:gemini-1.5-pro}")
    private String chatModel;

    private ChatLanguageModel model;
    private Map<String, String> chatHistory = new HashMap<>();

    @PostConstruct
    public void initialize() {
        if (projectId != null && !projectId.isEmpty()) {
            try {
                model = VertexAiGeminiChatModel.builder()
                    .project(projectId)
                    .location(location)
                    .modelName(chatModel)
                    .temperature(0.7f)
                    .maxOutputTokens(2048)
                    .build();
                log.info("Initialized Gemini chat model: {}", chatModel);
            } catch (Exception e) {
                log.error("Error initializing Gemini model", e);
            }
        } else {
            log.warn("Vertex AI project ID not configured, chat will use mock responses");
        }
    }

    public String chat(String message, String sessionId, String context) {
        if (model == null) {
            return "Chat service is not configured. Please set up Vertex AI credentials.";
        }

        try {
            String prompt = buildPrompt(message, sessionId, context);
            String response = model.generate(prompt);
            
            // Store in session history
            chatHistory.put(sessionId + "_last", message);
            
            return response;
        } catch (Exception e) {
            log.error("Error in chat service", e);
            return "Sorry, I encountered an error processing your message.";
        }
    }

    private String buildPrompt(String message, String sessionId, String context) {
        StringBuilder prompt = new StringBuilder();
        
        prompt.append("You are an AI assistant helping developers understand codebases.\n\n");
        
        if (context != null && !context.isEmpty()) {
            prompt.append("Context from codebase:\n");
            prompt.append(context);
            prompt.append("\n\n");
        }
        
        String lastMessage = chatHistory.get(sessionId + "_last");
        if (lastMessage != null) {
            prompt.append("Previous question: ").append(lastMessage).append("\n\n");
        }
        
        prompt.append("User question: ").append(message);
        
        return prompt.toString();
    }
}
