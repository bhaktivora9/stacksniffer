# StackSniffer

AI-Powered Tech Stack and Domain Detection for GitHub Repositories

## Overview

StackSniffer is a Spring Boot 3.2 application that uses AI to analyze GitHub repositories, providing intelligent code search, tech stack detection, and domain tagging capabilities.

## Features

- **Code Ingestion**: Fetch and process code from GitHub repositories
- **Hybrid Search**: Combine semantic (vector) and keyword search using Elasticsearch
- **AI-Powered Chat**: Ask questions about codebases using RAG (Retrieval Augmented Generation)
- **Tech Stack Analysis**: Automatically detect frameworks, libraries, and tools
- **Domain Tagging**: Identify domain patterns (e-commerce, finance, healthcare, etc.)

## Technology Stack

- **Java 17** with Spring Boot 3.2
- **Elasticsearch 8.11** with hybrid 768-dimensional vector search
- **Google Vertex AI** (Text Embedding Gecko + Gemini 1.5 Pro)
- **LangChain4j** for AI agent orchestration
- **JavaParser** for code analysis
- **GitHub API** for repository access
- **Lombok** for code generation
- **Maven** for build management

## Prerequisites

- Java 17 or higher
- Maven 3.6+
- Elasticsearch 8.11+ (running locally or remote)
- Google Cloud account with Vertex AI enabled
- GitHub personal access token (optional, for higher rate limits)

## Configuration

Create or edit `src/main/resources/application.properties`:

```properties
# Server Configuration
server.port=8080

# Elasticsearch Configuration
elasticsearch.host=localhost
elasticsearch.port=9200
elasticsearch.username=
elasticsearch.password=

# Vertex AI Configuration
vertexai.project-id=your-gcp-project-id
vertexai.location=us-central1
vertexai.embedding-model=textembedding-gecko@003
vertexai.chat-model=gemini-1.5-pro

# GitHub Configuration  
github.token=your-github-token
```

## Building the Application

```bash
mvn clean install
```

## Running the Application

```bash
mvn spring-boot:run
```

Or run the JAR:

```bash
java -jar target/stacksniffer-0.0.1-SNAPSHOT.jar
```

## API Endpoints

### 1. Ingest Repository

Fetch and index code from a GitHub repository:

```bash
POST /api/ingest
Content-Type: application/json

{
  "repositoryUrl": "https://github.com/owner/repo",
  "branch": "main",
  "includeTests": false
}
```

### 2. Search Code

Search for code using hybrid semantic + keyword search:

```bash
POST /api/search
Content-Type: application/json

{
  "query": "authentication logic",
  "topK": 10,
  "language": "java",
  "tags": ["service"]
}
```

### 3. Chat with Codebase

Ask questions about a codebase using AI:

```bash
POST /api/chat
Content-Type: application/json

{
  "message": "How does authentication work in this codebase?",
  "repositoryUrl": "https://github.com/owner/repo",
  "sessionId": "user-session-123"
}
```

### 4. Analyze Tech Stack

Get AI-powered analysis of a repository's tech stack:

```bash
GET /api/analyze/url?url=https://github.com/owner/repo
```

## Architecture

### Services

- **ElasticsearchService**: Manages the code_chunks index with 768-dimensional vector fields
- **VertexAIEmbeddingService**: Generates embeddings using Google's Gecko model
- **AgentService**: LangChain4j-based AI agent using Gemini 1.5 Pro
- **GitHubAPIService**: Fetches repository files and metadata
- **CodeParserService**: Parses code into chunks using JavaParser
- **DomainTaggerService**: Tags code with domain-specific labels
- **RAGService**: Implements Retrieval Augmented Generation
- **TechAnalyzerService**: Detects tech stack and frameworks
- **IngestionService**: Orchestrates the ingestion pipeline

### Models

- **CodeChunk**: Represents a code snippet with vector embedding and tags
- **SearchRequest/SearchResponse**: Search query and results
- **TechStackAnalysis**: Tech stack detection results
- **IngestRequest**: Repository ingestion parameters
- **ChatRequest**: Chat query parameters

## Elasticsearch Index

The `code_chunks` index schema:

```json
{
  "mappings": {
    "properties": {
      "id": { "type": "keyword" },
      "repositoryUrl": { "type": "keyword" },
      "filePath": { "type": "text" },
      "code": { "type": "text" },
      "vector": {
        "type": "dense_vector",
        "dims": 768,
        "index": true,
        "similarity": "cosine"
      },
      "tags": { "type": "keyword" },
      "language": { "type": "keyword" },
      "startLine": { "type": "integer" },
      "endLine": { "type": "integer" }
    }
  }
}
```

## License

MIT License - see LICENSE file for details

## Author

Bhakti Vora
