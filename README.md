# StackSniffer

AI-Powered Tech Stack and Domain Detection for GitHub Repositories

## Overview

This is a boilerplate Spring Boot application for building an AI-powered tech stack and domain detection system for GitHub repositories.

## Project Structure

```
stacksniffer/
├── src/
│   ├── main/
│   │   ├── java/
│   │   │   └── io/
│   │   │       └── stacksniffer/
│   │   │           └── StackSnifferApplication.java
│   │   └── resources/
│   │       └── application.properties
│   └── test/
│       └── java/
│           └── io/
│               └── stacksniffer/
│                   └── StackSnifferApplicationTests.java
├── pom.xml
├── .gitignore
├── LICENSE
└── README.md
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

## Next Steps

This is a skeleton project. To implement the full functionality, you'll need to add:

1. Controllers for API endpoints
2. Services for business logic
3. Models/DTOs for data structures
4. Configuration for external services
5. Additional dependencies as needed

## License

MIT License - see LICENSE file for details
