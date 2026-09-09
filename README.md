# StackSniffer

AI-Powered Tech Stack and Domain Detection for GitHub Repositories

## Overview

StackSniffer provides independent Java Spring Boot and Python FastAPI service shells for building an AI-powered tech stack and domain detection system.

## Project Structure

```text
stacksniffer/
├── services/
│   ├── gateway-service/                 # Java / Spring Boot
│   ├── repository-service/             # Java / Spring Boot
│   ├── governance-service/             # Java / Spring Boot
│   ├── agent-service/                  # Python / FastAPI
│   └── graph-intelligence-service/     # Python / FastAPI
├── contracts/
├── infrastructure/
├── tests/
└── docs/
```

## Toolchain

- Java 17
- Maven 3.6 or newer
- Python 3.13.7, recorded in `.python-version`
- Spring Boot 3.2.12
- Python dependencies managed with `pyproject.toml`, setuptools, and pip
- Python tests run with pytest

## Java Commands

Run each Java service from the repository root:

```powershell
mvn -f services/gateway-service/pom.xml clean verify
mvn -f services/repository-service/pom.xml clean verify
mvn -f services/governance-service/pom.xml clean verify
```

Start a Java service independently:

```powershell
mvn -f services/gateway-service/pom.xml spring-boot:run
mvn -f services/repository-service/pom.xml spring-boot:run
mvn -f services/governance-service/pom.xml spring-boot:run
```

The Java health endpoints are available at `/actuator/health` on ports `8081`,
`8082`, and `8083` respectively.

## Python Commands

Install and test each Python service from the repository root:

```powershell
python -m pip install -e "services/agent-service[test]"
python -m pytest services/agent-service/tests

python -m pip install -e "services/graph-intelligence-service[test]"
python -m pytest services/graph-intelligence-service/tests
```

Start a Python service independently:

```powershell
python -m uvicorn agent_service.main:app --app-dir services/agent-service/src --port 8084
python -m uvicorn graph_intelligence_service.main:app --app-dir services/graph-intelligence-service/src --port 8085
```

The Python health endpoints are available at `/health` on ports `8084` and
`8085` respectively.

## M6 Frontend Roadmap

The frontend work is organized into these milestones:

- **M6-E1 Frontend Bootstrap**: establish the React application, build tooling, and shared layout.
- **M6-E2 Repository Analysis UI**: expose repository ingestion, analysis status, and findings.
- **M6-E3 Architecture Review UI**: present architecture summaries, risks, and review workflows.
- **M6-E4 Graph / Relationship Visualization**: visualize service, dependency, and domain relationships.
- **M6-E5 Governance UI**: expose policy, compliance, and governance findings.
- **M6-E6 Streaming / SSE UX**: provide progressive updates for long-running analysis.
- **M6-E7 Authentication + Session UX**: add sign-in, session state, and protected application flows.

### Frontend Foundation

The frontend foundation will use:

- React with TypeScript
- Vite or Next.js for the application toolchain
- Client-side routing
- A typed API client for backend services
- An authentication and session shell
- A shared design system
- Consistent error handling and loading states
- An SSE client for streaming analysis updates
- Component, integration, and end-to-end testing

## Technology Stack

- Java 17
- Spring Boot 3.2.12
- Maven
- Python 3.13.7
- FastAPI
- Uvicorn
- pytest

## License

MIT License - see LICENSE file for details
