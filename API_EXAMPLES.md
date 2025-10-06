# StackSniffer API Examples

## Prerequisites

1. Start Elasticsearch 8.11+
2. Configure Vertex AI credentials (optional for testing)
3. Set GitHub token (optional for testing)
4. Start the application: `mvn spring-boot:run`

## Example API Calls

### 1. Ingest a Repository

```bash
curl -X POST http://localhost:8080/api/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "repositoryUrl": "https://github.com/spring-projects/spring-petclinic",
    "branch": "main",
    "includeTests": false
  }'
```

**Response:**
```json
{
  "status": "success",
  "message": "Repository ingestion started",
  "repositoryUrl": "https://github.com/spring-projects/spring-petclinic"
}
```

### 2. Search for Code

```bash
curl -X POST http://localhost:8080/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "database connection",
    "topK": 5,
    "language": "java"
  }'
```

**Response:**
```json
{
  "results": [
    {
      "id": "abc123",
      "repositoryUrl": "https://github.com/spring-projects/spring-petclinic",
      "filePath": "src/main/java/config/DatabaseConfig.java",
      "code": "...",
      "vector": [...],
      "tags": ["configuration", "database"],
      "language": "java",
      "startLine": 10,
      "endLine": 50
    }
  ],
  "totalResults": 5,
  "searchTimeMs": 123
}
```

### 3. Search with Filters

```bash
curl -X POST http://localhost:8080/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "user authentication",
    "topK": 10,
    "language": "java",
    "tags": ["service", "security"],
    "minScore": 0.7
  }'
```

### 4. Chat with AI Agent

```bash
curl -X POST http://localhost:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "How is the database configured in this project?",
    "repositoryUrl": "https://github.com/spring-projects/spring-petclinic",
    "sessionId": "user-session-123"
  }'
```

**Response:**
```json
{
  "answer": "The database is configured using Spring Boot's auto-configuration...",
  "status": "success"
}
```

### 5. Analyze Tech Stack

```bash
curl -X GET "http://localhost:8080/api/analyze/url?url=https://github.com/spring-projects/spring-petclinic"
```

**Response:**
```json
{
  "repositoryUrl": "https://github.com/spring-projects/spring-petclinic",
  "languages": ["java", "html", "css"],
  "frameworks": ["Spring Boot", "Spring Web", "Hibernate"],
  "libraries": ["Lombok", "Jackson"],
  "tools": ["Maven"],
  "domainTags": {
    "e-commerce": "0.15",
    "healthcare": "0.30"
  },
  "primaryDomain": "healthcare",
  "confidenceScore": 0.85,
  "summary": "This repository primarily uses java, html, css. Key frameworks include Spring Boot, Spring Web, Hibernate. The codebase appears to be in the healthcare domain."
}
```

## Testing with cURL Scripts

### Complete workflow example:

```bash
#!/bin/bash

BASE_URL="http://localhost:8080"
REPO_URL="https://github.com/spring-projects/spring-petclinic"

# 1. Ingest repository
echo "Ingesting repository..."
curl -X POST $BASE_URL/api/ingest \
  -H "Content-Type: application/json" \
  -d "{
    \"repositoryUrl\": \"$REPO_URL\",
    \"branch\": \"main\",
    \"includeTests\": false
  }"

echo -e "\n\nWaiting for ingestion to complete...\n"
sleep 30

# 2. Search for code
echo "Searching for database-related code..."
curl -X POST $BASE_URL/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "database connection",
    "topK": 3,
    "language": "java"
  }' | jq

echo -e "\n\n"

# 3. Analyze tech stack
echo "Analyzing tech stack..."
curl -X GET "$BASE_URL/api/analyze/url?url=$REPO_URL" | jq

echo -e "\n\n"

# 4. Chat with AI
echo "Asking AI about the codebase..."
curl -X POST $BASE_URL/api/chat \
  -H "Content-Type: application/json" \
  -d "{
    \"message\": \"What design patterns are used in this codebase?\",
    \"repositoryUrl\": \"$REPO_URL\",
    \"sessionId\": \"test-session\"
  }" | jq
```

Save this as `test-api.sh`, make it executable with `chmod +x test-api.sh`, and run it with `./test-api.sh`.

## Common Query Patterns

### Search by Domain
```bash
curl -X POST http://localhost:8080/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "payment processing",
    "tags": ["e-commerce"],
    "topK": 10
  }'
```

### Search by Framework
```bash
curl -X POST http://localhost:8080/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "REST controller endpoints",
    "tags": ["rest-controller"],
    "language": "java",
    "topK": 5
  }'
```

### Multi-turn Chat Conversation
```bash
# First message
curl -X POST http://localhost:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is the main purpose of this codebase?",
    "repositoryUrl": "https://github.com/owner/repo",
    "sessionId": "conv-123"
  }'

# Follow-up message (uses same sessionId)
curl -X POST http://localhost:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Can you show me examples of how it implements that?",
    "repositoryUrl": "https://github.com/owner/repo",
    "sessionId": "conv-123"
  }'
```

## Error Handling

All endpoints return appropriate HTTP status codes:

- `200 OK` - Success
- `400 Bad Request` - Invalid request parameters
- `500 Internal Server Error` - Server-side error

Error response format:
```json
{
  "status": "error",
  "message": "Error description here"
}
```

## Performance Tips

1. **Ingestion**: Large repositories may take several minutes to ingest
2. **Search**: First search after ingestion may be slower as vectors are loaded
3. **Chat**: Response time depends on Vertex AI API latency (~2-5 seconds)
4. **Batch Operations**: For multiple repositories, ingest them sequentially

## Rate Limits

- **GitHub API**: 60 requests/hour (anonymous), 5000 requests/hour (authenticated)
- **Vertex AI**: Depends on your GCP quota
- **Elasticsearch**: No built-in rate limits (depends on cluster capacity)
