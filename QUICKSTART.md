# StackSniffer Quick Start Guide

Get up and running with StackSniffer in 5 minutes!

## Prerequisites

Ensure you have installed:
- ☐ Java 17 or higher
- ☐ Maven 3.6+
- ☐ Elasticsearch 8.11+ (Docker recommended)

## Step 1: Start Elasticsearch

Using Docker (recommended):

```bash
docker run -d \
  --name elasticsearch \
  -p 9200:9200 \
  -p 9300:9300 \
  -e "discovery.type=single-node" \
  -e "xpack.security.enabled=false" \
  docker.elastic.co/elasticsearch/elasticsearch:8.11.0
```

Verify Elasticsearch is running:
```bash
curl http://localhost:9200
```

## Step 2: Clone and Build

```bash
git clone https://github.com/bhaktivora9/stacksniffer.git
cd stacksniffer
mvn clean install
```

Build time: ~30 seconds

## Step 3: Configure (Optional)

For production use, edit `src/main/resources/application.properties`:

```properties
# Vertex AI (optional - for AI features)
vertexai.project-id=your-gcp-project-id

# GitHub (optional - for higher rate limits)
github.token=your-github-token
```

For quick testing, you can skip this step. The app will work with:
- Anonymous GitHub access (60 requests/hour)
- Dummy embeddings (for testing without Vertex AI)

## Step 4: Run the Application

```bash
mvn spring-boot:run
```

Or run the JAR directly:
```bash
java -jar target/stacksniffer-0.0.1-SNAPSHOT.jar
```

Wait for the startup message:
```
Started StackSnifferApplication in X.XXX seconds
```

## Step 5: Test the API

Open a new terminal and test the endpoints:

### Test 1: Health Check
```bash
curl http://localhost:8080/actuator/health 2>/dev/null || echo "App is running on port 8080"
```

### Test 2: Ingest a Small Repository
```bash
curl -X POST http://localhost:8080/api/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "repositoryUrl": "https://github.com/spring-guides/gs-rest-service",
    "branch": "main",
    "includeTests": false
  }'
```

Expected response:
```json
{
  "status": "success",
  "message": "Repository ingestion started",
  "repositoryUrl": "https://github.com/spring-guides/gs-rest-service"
}
```

Wait 30-60 seconds for ingestion to complete.

### Test 3: Search for Code
```bash
curl -X POST http://localhost:8080/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "rest controller",
    "topK": 3
  }'
```

You should see code chunks from the ingested repository!

### Test 4: Analyze Tech Stack
```bash
curl -X GET "http://localhost:8080/api/analyze/url?url=https://github.com/spring-guides/gs-rest-service"
```

Expected response includes:
- Languages detected
- Frameworks found (e.g., Spring Boot)
- Domain classification

## Common Issues & Solutions

### Issue: "Connection refused" to Elasticsearch
**Solution**: Make sure Elasticsearch is running on port 9200
```bash
docker ps | grep elasticsearch
curl http://localhost:9200
```

### Issue: "Rate limit exceeded" from GitHub
**Solution**: Either:
1. Wait an hour for the rate limit to reset, or
2. Add a GitHub token to `application.properties`

### Issue: Application starts but searches return empty
**Solution**: Wait for the ingestion to complete (check logs) before searching

## What's Next?

### Try More Features

1. **Chat with AI** (requires Vertex AI setup):
   ```bash
   curl -X POST http://localhost:8080/api/chat \
     -H "Content-Type: application/json" \
     -d '{
       "message": "What does this codebase do?",
       "repositoryUrl": "https://github.com/spring-guides/gs-rest-service",
       "sessionId": "test-session"
     }'
   ```

2. **Ingest More Repositories**:
   - Try larger repos like `spring-projects/spring-petclinic`
   - Or your own repositories

3. **Advanced Searches**:
   ```bash
   curl -X POST http://localhost:8080/api/search \
     -H "Content-Type: application/json" \
     -d '{
       "query": "database configuration",
       "language": "java",
       "tags": ["configuration"],
       "topK": 5
     }'
   ```

### Enable Full AI Features

To enable Vertex AI (for semantic search and chat):

1. Set up Google Cloud Project
2. Enable Vertex AI API
3. Set up authentication:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
   ```
4. Update `application.properties` with your project ID
5. Restart the application

See [README.md](README.md) for detailed setup instructions.

### Read the Documentation

- **[README.md](README.md)** - Complete documentation
- **[API_EXAMPLES.md](API_EXAMPLES.md)** - All API endpoints with examples
- **[IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)** - Architecture details

## Troubleshooting

### Check Logs
```bash
tail -f logs/spring.log
```

### Verify Dependencies
```bash
mvn dependency:tree | grep -E "elasticsearch|langchain|vertex"
```

### Test Elasticsearch Directly
```bash
# Check if index was created
curl http://localhost:9200/_cat/indices?v

# View index mapping
curl http://localhost:9200/code_chunks/_mapping?pretty

# Count documents
curl http://localhost:9200/code_chunks/_count
```

## Performance Tips

1. **Ingestion**: Start with small repos (<100 files) for testing
2. **Elasticsearch**: Allocate at least 2GB RAM for Docker container
3. **JVM**: For large repos, increase heap: `java -Xmx4g -jar stacksniffer.jar`

## Getting Help

- Check the [README.md](README.md) for detailed documentation
- Review [API_EXAMPLES.md](API_EXAMPLES.md) for usage examples
- Look at [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md) for architecture

## Success Checklist

- ✅ Elasticsearch is running and accessible
- ✅ Application starts without errors
- ✅ Successfully ingested a repository
- ✅ Search returns results
- ✅ Tech stack analysis works

Congratulations! You're now ready to use StackSniffer! 🎉
