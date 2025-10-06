package io.stacksniffer.controller;

import io.stacksniffer.model.IngestRequest;
import io.stacksniffer.service.IngestionService;
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
public class IngestionController {

    private final IngestionService ingestionService;

    @PostMapping("/ingest")
    public ResponseEntity<Map<String, Object>> ingestRepository(@RequestBody IngestRequest request) {
        Map<String, Object> response = new HashMap<>();
        
        try {
            log.info("Received ingestion request for: {}", request.getRepositoryUrl());
            
            ingestionService.ingestRepository(
                request.getRepositoryUrl(),
                request.getBranch(),
                request.getIncludeTests()
            );
            
            response.put("status", "success");
            response.put("message", "Repository ingestion started");
            response.put("repositoryUrl", request.getRepositoryUrl());
            
            return ResponseEntity.ok(response);
            
        } catch (Exception e) {
            log.error("Error ingesting repository", e);
            response.put("status", "error");
            response.put("message", e.getMessage());
            return ResponseEntity.internalServerError().body(response);
        }
    }
}
