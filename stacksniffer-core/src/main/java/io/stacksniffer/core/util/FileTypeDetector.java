package io.stacksniffer.core.util;

import java.util.HashMap;
import java.util.Map;

public class FileTypeDetector {

    private static final Map<String, String> EXTENSION_TO_LANGUAGE = new HashMap<>();

    static {
        EXTENSION_TO_LANGUAGE.put("java", "Java");
        EXTENSION_TO_LANGUAGE.put("kt", "Kotlin");
        EXTENSION_TO_LANGUAGE.put("scala", "Scala");
        EXTENSION_TO_LANGUAGE.put("groovy", "Groovy");

        EXTENSION_TO_LANGUAGE.put("py", "Python");
        EXTENSION_TO_LANGUAGE.put("pyw", "Python");

        EXTENSION_TO_LANGUAGE.put("js", "JavaScript");
        EXTENSION_TO_LANGUAGE.put("jsx", "JavaScript");
        EXTENSION_TO_LANGUAGE.put("mjs", "JavaScript");
        EXTENSION_TO_LANGUAGE.put("cjs", "JavaScript");

        EXTENSION_TO_LANGUAGE.put("ts", "TypeScript");
        EXTENSION_TO_LANGUAGE.put("tsx", "TypeScript");

        EXTENSION_TO_LANGUAGE.put("go", "Go");

        EXTENSION_TO_LANGUAGE.put("rs", "Rust");

        EXTENSION_TO_LANGUAGE.put("c", "C");
        EXTENSION_TO_LANGUAGE.put("h", "C");

        EXTENSION_TO_LANGUAGE.put("cpp", "C++");
        EXTENSION_TO_LANGUAGE.put("cc", "C++");
        EXTENSION_TO_LANGUAGE.put("cxx", "C++");
        EXTENSION_TO_LANGUAGE.put("hpp", "C++");

        EXTENSION_TO_LANGUAGE.put("cs", "C#");

        EXTENSION_TO_LANGUAGE.put("rb", "Ruby");

        EXTENSION_TO_LANGUAGE.put("php", "PHP");

        EXTENSION_TO_LANGUAGE.put("swift", "Swift");

        EXTENSION_TO_LANGUAGE.put("m", "Objective-C");

        EXTENSION_TO_LANGUAGE.put("sh", "Shell");
        EXTENSION_TO_LANGUAGE.put("bash", "Shell");

        EXTENSION_TO_LANGUAGE.put("sql", "SQL");

        EXTENSION_TO_LANGUAGE.put("html", "HTML");
        EXTENSION_TO_LANGUAGE.put("htm", "HTML");

        EXTENSION_TO_LANGUAGE.put("css", "CSS");
        EXTENSION_TO_LANGUAGE.put("scss", "SCSS");
        EXTENSION_TO_LANGUAGE.put("sass", "Sass");

        EXTENSION_TO_LANGUAGE.put("xml", "XML");

        EXTENSION_TO_LANGUAGE.put("json", "JSON");

        EXTENSION_TO_LANGUAGE.put("yaml", "YAML");
        EXTENSION_TO_LANGUAGE.put("yml", "YAML");

        EXTENSION_TO_LANGUAGE.put("md", "Markdown");
    }

    public static String detectLanguage(String filePath) {
        if (filePath == null || filePath.isEmpty()) {
            return "Unknown";
        }

        int lastDotIndex = filePath.lastIndexOf('.');
        if (lastDotIndex == -1 || lastDotIndex == filePath.length() - 1) {
            return "Unknown";
        }

        String extension = filePath.substring(lastDotIndex + 1).toLowerCase();
        return EXTENSION_TO_LANGUAGE.getOrDefault(extension, "Unknown");
    }

    public static boolean isCodeFile(String filePath) {
        String language = detectLanguage(filePath);
        return !language.equals("Unknown") && !language.equals("Markdown");
    }

    public static boolean isJavaFile(String filePath) {
        return "Java".equals(detectLanguage(filePath));
    }

    public static boolean isPythonFile(String filePath) {
        return "Python".equals(detectLanguage(filePath));
    }

    public static boolean isJavaScriptFile(String filePath) {
        String language = detectLanguage(filePath);
        return "JavaScript".equals(language) || "TypeScript".equals(language);
    }

    public static String getFileExtension(String filePath) {
        if (filePath == null || filePath.isEmpty()) {
            return "";
        }

        int lastDotIndex = filePath.lastIndexOf('.');
        if (lastDotIndex == -1 || lastDotIndex == filePath.length() - 1) {
            return "";
        }

        return filePath.substring(lastDotIndex + 1).toLowerCase();
    }
}
