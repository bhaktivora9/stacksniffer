package io.stacksniffer.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.*;

@Service
@Slf4j
public class DomainTaggerService {

    private static final Map<String, List<String>> DOMAIN_KEYWORDS = new HashMap<>();

    static {
        DOMAIN_KEYWORDS.put("e-commerce", Arrays.asList(
            "payment", "cart", "checkout", "order", "product", "inventory", "shipping"
        ));
        DOMAIN_KEYWORDS.put("finance", Arrays.asList(
            "transaction", "account", "balance", "loan", "credit", "debit", "bank"
        ));
        DOMAIN_KEYWORDS.put("healthcare", Arrays.asList(
            "patient", "doctor", "appointment", "medical", "diagnosis", "prescription"
        ));
        DOMAIN_KEYWORDS.put("education", Arrays.asList(
            "student", "course", "assignment", "grade", "teacher", "enrollment"
        ));
        DOMAIN_KEYWORDS.put("social-media", Arrays.asList(
            "post", "comment", "like", "follow", "friend", "feed", "profile"
        ));
        DOMAIN_KEYWORDS.put("data-analytics", Arrays.asList(
            "analytics", "metrics", "dashboard", "report", "visualization", "insight"
        ));
        DOMAIN_KEYWORDS.put("iot", Arrays.asList(
            "sensor", "device", "telemetry", "mqtt", "gateway", "edge"
        ));
        DOMAIN_KEYWORDS.put("ai-ml", Arrays.asList(
            "model", "training", "prediction", "embedding", "neural", "inference"
        ));
    }

    public Map<String, String> tagDomain(String code) {
        Map<String, String> domainScores = new HashMap<>();
        String lowerCode = code.toLowerCase();
        
        for (Map.Entry<String, List<String>> entry : DOMAIN_KEYWORDS.entrySet()) {
            String domain = entry.getKey();
            int matchCount = 0;
            
            for (String keyword : entry.getValue()) {
                if (lowerCode.contains(keyword)) {
                    matchCount++;
                }
            }
            
            if (matchCount > 0) {
                double score = (double) matchCount / entry.getValue().size();
                domainScores.put(domain, String.format("%.2f", score));
            }
        }
        
        return domainScores;
    }

    public String determinePrimaryDomain(Map<String, String> domainScores) {
        if (domainScores.isEmpty()) {
            return "general";
        }
        
        return domainScores.entrySet().stream()
            .max(Comparator.comparing(e -> Double.parseDouble(e.getValue())))
            .map(Map.Entry::getKey)
            .orElse("general");
    }

    public List<String> generateDomainTags(String code) {
        Map<String, String> domainScores = tagDomain(code);
        List<String> tags = new ArrayList<>();
        
        for (Map.Entry<String, String> entry : domainScores.entrySet()) {
            if (Double.parseDouble(entry.getValue()) > 0.2) {
                tags.add(entry.getKey());
            }
        }
        
        return tags;
    }
}
