package io.stacksniffer.controller;

import io.stacksniffer.model.ChatRequest;
import io.stacksniffer.service.RAGService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.HashMap;
import java.util.Map;

@RestController
@RequestMapping("/api")
@RequiredArgsConstructor
@Slf4j
public class ChatController {

    private final RAGService ragService;

    @PostMapping("/chat")
    public ResponseEntity<Map<String, String>> chat(@RequestBody ChatRequest request) {
        Map<String, String> response = new HashMap<>();
        
        try {
            log.info("Received chat request: {}", request.getMessage());
            
            String sessionId = request.getSessionId() != null ? 
                request.getSessionId() : "default";
            
            String answer = ragService.retrieveAndGenerate(
                request.getMessage(),
                request.getRepositoryUrl(),
                sessionId
            );
            
            response.put("answer", answer);
            response.put("status", "success");
            
            return ResponseEntity.ok(response);
            
        } catch (Exception e) {
            log.error("Error in chat", e);
            response.put("status", "error");
            response.put("message", e.getMessage());
            return ResponseEntity.internalServerError().body(response);
        }
    }
}
