package io.stacksniffer.core.model;

/**
 * Represents a chunk of code from a repository
 */
public class CodeChunk {
    
    private String id;
    private String filePath;
    private String content;
    private int startLine;
    private int endLine;
    private String language;

    public CodeChunk() {
    }

    public CodeChunk(String id, String filePath, String content, int startLine, int endLine, String language) {
        this.id = id;
        this.filePath = filePath;
        this.content = content;
        this.startLine = startLine;
        this.endLine = endLine;
        this.language = language;
    }

    public String getId() {
        return id;
    }

    public void setId(String id) {
        this.id = id;
    }

    public String getFilePath() {
        return filePath;
    }

    public void setFilePath(String filePath) {
        this.filePath = filePath;
    }

    public String getContent() {
        return content;
    }

    public void setContent(String content) {
        this.content = content;
    }

    public int getStartLine() {
        return startLine;
    }

    public void setStartLine(int startLine) {
        this.startLine = startLine;
    }

    public int getEndLine() {
        return endLine;
    }

    public void setEndLine(int endLine) {
        this.endLine = endLine;
    }

    public String getLanguage() {
        return language;
    }

    public void setLanguage(String language) {
        this.language = language;
    }
}
