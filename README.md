# StackSniffer

AI-Powered Tech Stack and Domain Detection for GitHub Repositories

## Overview

This is a boilerplate Spring Boot application for building an AI-powered tech stack and domain detection system for GitHub repositories.

## Project Structure

```stacksniffer/
├── stacksniffer-parent/         # Maven parent POM
├── stacksniffer-core/           # Domain models, DTOs, events 
├── stacksniffer-config/         # YAML pattern configurations
├── stacksniffer-ingestion/      # GitHub API, file analysis
├── stacksniffer-search/         # Elasticsearch integration
├── stacksniffer-ai/             # Vertex AI, Gemini, embeddings, RAG
├── stacksniffer-learning/       # Self-learning ML pipeline
├── stacksniffer-agents/         # Google ADK agents
└── stacksniffer-api/            # REST controllers, Spring Boot app
```

## Prerequisites

- Java 17 or higher
- Maven 3.6+

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

## Running Tests

```bash
mvn test
```

## Technology Stack

- Java 17
- Spring Boot 3.2
- Maven

## License

MIT License - see LICENSE file for details
