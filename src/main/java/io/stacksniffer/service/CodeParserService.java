package io.stacksniffer.service;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import io.stacksniffer.model.CodeChunk;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.*;

@Service
@Slf4j
public class CodeParserService {

    private final JavaParser javaParser = new JavaParser();

    public List<CodeChunk> parseCodeFile(String filePath, String content, String repositoryUrl) {
        List<CodeChunk> chunks = new ArrayList<>();
        
        String language = detectLanguage(filePath);
        
        if ("java".equalsIgnoreCase(language)) {
            chunks.addAll(parseJavaFile(filePath, content, repositoryUrl));
        } else {
            // For non-Java files, create chunks by splitting on logical boundaries
            chunks.addAll(parseGenericFile(filePath, content, repositoryUrl, language));
        }
        
        return chunks;
    }

    private List<CodeChunk> parseJavaFile(String filePath, String content, String repositoryUrl) {
        List<CodeChunk> chunks = new ArrayList<>();
        
        try {
            CompilationUnit cu = javaParser.parse(content).getResult().orElse(null);
            if (cu == null) {
                return parseGenericFile(filePath, content, repositoryUrl, "java");
            }
            
            // Extract classes
            cu.findAll(ClassOrInterfaceDeclaration.class).forEach(cls -> {
                CodeChunk chunk = CodeChunk.builder()
                    .repositoryUrl(repositoryUrl)
                    .filePath(filePath)
                    .code(cls.toString())
                    .language("java")
                    .startLine(cls.getBegin().map(pos -> pos.line).orElse(0))
                    .endLine(cls.getEnd().map(pos -> pos.line).orElse(0))
                    .tags(extractJavaTags(cls.toString()))
                    .metadata(new HashMap<>())
                    .build();
                chunks.add(chunk);
            });
            
            // Extract methods
            cu.findAll(MethodDeclaration.class).forEach(method -> {
                CodeChunk chunk = CodeChunk.builder()
                    .repositoryUrl(repositoryUrl)
                    .filePath(filePath)
                    .code(method.toString())
                    .language("java")
                    .startLine(method.getBegin().map(pos -> pos.line).orElse(0))
                    .endLine(method.getEnd().map(pos -> pos.line).orElse(0))
                    .tags(extractJavaTags(method.toString()))
                    .metadata(new HashMap<>())
                    .build();
                chunks.add(chunk);
            });
            
        } catch (Exception e) {
            log.error("Error parsing Java file: {}", filePath, e);
            return parseGenericFile(filePath, content, repositoryUrl, "java");
        }
        
        return chunks;
    }

    private List<CodeChunk> parseGenericFile(String filePath, String content, 
                                             String repositoryUrl, String language) {
        List<CodeChunk> chunks = new ArrayList<>();
        
        // Split by lines and create chunks of ~50 lines each
        String[] lines = content.split("\n");
        int chunkSize = 50;
        
        for (int i = 0; i < lines.length; i += chunkSize) {
            int endLine = Math.min(i + chunkSize, lines.length);
            String chunkContent = String.join("\n", 
                Arrays.copyOfRange(lines, i, endLine));
            
            CodeChunk chunk = CodeChunk.builder()
                .repositoryUrl(repositoryUrl)
                .filePath(filePath)
                .code(chunkContent)
                .language(language)
                .startLine(i + 1)
                .endLine(endLine)
                .tags(extractGenericTags(chunkContent, language))
                .metadata(new HashMap<>())
                .build();
            
            chunks.add(chunk);
        }
        
        return chunks;
    }

    private List<String> extractJavaTags(String code) {
        List<String> tags = new ArrayList<>();
        
        if (code.contains("@Service")) tags.add("service");
        if (code.contains("@Controller")) tags.add("controller");
        if (code.contains("@RestController")) tags.add("rest-controller");
        if (code.contains("@Repository")) tags.add("repository");
        if (code.contains("@Component")) tags.add("component");
        if (code.contains("@Configuration")) tags.add("configuration");
        if (code.contains("public class")) tags.add("class");
        if (code.contains("interface")) tags.add("interface");
        if (code.contains("public ") || code.contains("private ") || code.contains("protected ")) {
            tags.add("method");
        }
        
        return tags;
    }

    private List<String> extractGenericTags(String code, String language) {
        List<String> tags = new ArrayList<>();
        tags.add(language);
        
        // Common patterns
        if (code.contains("function") || code.contains("def ") || code.contains("func ")) {
            tags.add("function");
        }
        if (code.contains("class ")) tags.add("class");
        if (code.contains("import ") || code.contains("require")) tags.add("imports");
        
        return tags;
    }

    private String detectLanguage(String filePath) {
        String lower = filePath.toLowerCase();
        if (lower.endsWith(".java")) return "java";
        if (lower.endsWith(".py")) return "python";
        if (lower.endsWith(".js")) return "javascript";
        if (lower.endsWith(".ts")) return "typescript";
        if (lower.endsWith(".go")) return "go";
        if (lower.endsWith(".rs")) return "rust";
        if (lower.endsWith(".cpp") || lower.endsWith(".cc")) return "cpp";
        if (lower.endsWith(".c")) return "c";
        if (lower.endsWith(".cs")) return "csharp";
        if (lower.endsWith(".rb")) return "ruby";
        if (lower.endsWith(".php")) return "php";
        if (lower.endsWith(".kt")) return "kotlin";
        if (lower.endsWith(".swift")) return "swift";
        return "unknown";
    }
}
