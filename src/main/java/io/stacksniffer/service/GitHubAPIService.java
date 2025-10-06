package io.stacksniffer.service;

import lombok.extern.slf4j.Slf4j;
import org.kohsuke.github.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.util.*;

@Service
@Slf4j
public class GitHubAPIService {

    @Value("${github.token:}")
    private String githubToken;

    private GitHub github;

    private GitHub getGitHub() throws IOException {
        if (github == null) {
            if (githubToken != null && !githubToken.isEmpty()) {
                github = new GitHubBuilder().withOAuthToken(githubToken).build();
            } else {
                github = GitHub.connectAnonymously();
                log.warn("Using anonymous GitHub access - rate limits will be restrictive");
            }
        }
        return github;
    }

    public Map<String, String> fetchRepositoryFiles(String repositoryUrl, String branch) throws IOException {
        Map<String, String> files = new HashMap<>();
        
        try {
            String[] parts = parseRepositoryUrl(repositoryUrl);
            if (parts == null) {
                throw new IllegalArgumentException("Invalid repository URL: " + repositoryUrl);
            }
            
            String owner = parts[0];
            String repo = parts[1];
            
            GHRepository repository = getGitHub().getRepository(owner + "/" + repo);
            
            String ref = branch != null ? branch : repository.getDefaultBranch();
            GHTree tree = repository.getTreeRecursive(ref, 1);
            
            for (GHTreeEntry entry : tree.getTree()) {
                if ("blob".equals(entry.getType()) && isCodeFile(entry.getPath())) {
                    try {
                        GHContent content = repository.getFileContent(entry.getPath(), ref);
                        if (content != null && !content.isDirectory()) {
                            files.put(entry.getPath(), content.getContent());
                        }
                    } catch (IOException e) {
                        log.debug("Could not fetch content for: {}", entry.getPath());
                    }
                }
            }
            
            log.info("Fetched {} files from {}", files.size(), repositoryUrl);
        } catch (Exception e) {
            log.error("Error fetching repository files", e);
            throw e;
        }
        
        return files;
    }

    public Map<String, Object> getRepositoryMetadata(String repositoryUrl) throws IOException {
        Map<String, Object> metadata = new HashMap<>();
        
        try {
            String[] parts = parseRepositoryUrl(repositoryUrl);
            if (parts == null) {
                return metadata;
            }
            
            String owner = parts[0];
            String repo = parts[1];
            
            GHRepository repository = getGitHub().getRepository(owner + "/" + repo);
            
            metadata.put("name", repository.getName());
            metadata.put("description", repository.getDescription());
            metadata.put("language", repository.getLanguage());
            metadata.put("stars", repository.getStargazersCount());
            metadata.put("forks", repository.getForksCount());
            metadata.put("topics", repository.listTopics());
            
        } catch (Exception e) {
            log.error("Error fetching repository metadata", e);
        }
        
        return metadata;
    }

    private String[] parseRepositoryUrl(String url) {
        // Parse URLs like: https://github.com/owner/repo or github.com/owner/repo
        String pattern = "(?:https?://)?(?:www\\.)?github\\.com/([^/]+)/([^/]+?)(?:\\.git)?(?:/.*)?$";
        java.util.regex.Pattern p = java.util.regex.Pattern.compile(pattern);
        java.util.regex.Matcher m = p.matcher(url);
        
        if (m.find()) {
            return new String[]{m.group(1), m.group(2)};
        }
        return null;
    }

    private boolean isCodeFile(String path) {
        String lowerPath = path.toLowerCase();
        return lowerPath.endsWith(".java") || 
               lowerPath.endsWith(".py") || 
               lowerPath.endsWith(".js") || 
               lowerPath.endsWith(".ts") || 
               lowerPath.endsWith(".go") || 
               lowerPath.endsWith(".rs") || 
               lowerPath.endsWith(".cpp") || 
               lowerPath.endsWith(".c") || 
               lowerPath.endsWith(".cs") || 
               lowerPath.endsWith(".rb") || 
               lowerPath.endsWith(".php") || 
               lowerPath.endsWith(".kt") || 
               lowerPath.endsWith(".swift");
    }
}
