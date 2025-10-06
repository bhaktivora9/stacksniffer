package io.stacksniffer.controller;

import io.stacksniffer.model.TechStackAnalysis;
import io.stacksniffer.service.TechAnalyzerService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/analyze")
@RequiredArgsConstructor
@Slf4j
public class AnalysisController {

    private final TechAnalyzerService techAnalyzerService;

    @GetMapping("/url")
    public ResponseEntity<TechStackAnalysis> analyzeUrl(@RequestParam String url) {
        try {
            log.info("Received analysis request for: {}", url);
            
            TechStackAnalysis analysis = techAnalyzerService.analyzeRepository(url);
            
            return ResponseEntity.ok(analysis);
            
        } catch (Exception e) {
            log.error("Error analyzing repository", e);
            return ResponseEntity.internalServerError().build();
        }
    }
}
