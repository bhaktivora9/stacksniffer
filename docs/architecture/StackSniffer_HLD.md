**StackSniffer**

**Signed-Off High-Level Service Architecture**

Services • Components • Data Ownership • Communication • Storage • Cross-Cutting Decisions

**Purpose: freeze the service/component model and all HLD decisions made so far before moving to Low-Level Design. This document is the architecture handoff point; no epics, stories, class diagrams, DTOs, database schemas, or event payload schemas are defined here.**

**Status: SIGNED OFF FOR HLD • LLD NEXT**

# 1\. Signed-Off Decision Register

The following decisions are considered approved HLD choices unless explicitly marked later/deferred.

| **Decision**                | **Approved direction**                                                                                                          | **Status**     |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| **Architecture style**      | Microservices with a bounded service count; do not create one deployment per agent/capability.                                  | **SIGNED OFF** |
| **Primary backend runtime** | Java + Spring Boot for platform, repository and governance services.                                                            | **SIGNED OFF** |
| **AI runtime**              | Python + FastAPI; LangGraph is the primary production agent runtime.                                                            | **SIGNED OFF** |
| **Operational database**    | MongoDB remains the primary operational/source-of-truth database.                                                               | **SIGNED OFF** |
| **Cache / coordination**    | Redis for caching, rate limiting, request coalescing and short-lived coordination; never canonical truth.                       | **SIGNED OFF** |
| **Graph database**          | Neo4j is selected for the knowledge-graph/intelligence projection.                                                              | **SIGNED OFF** |
| **Graph authority**         | Neo4j is rebuildable intelligence projection; MongoDB/governance state remains canonical.                                       | **SIGNED OFF** |
| **Object storage**          | Google Cloud Storage (GCS).                                                                                                     | **SIGNED OFF** |
| **Repository retention**    | Ephemeral clone + persist derived/normalized artifacts; full source snapshot in GCS is optional, not default.                   | **SIGNED OFF** |
| **Kafka**                   | Kafka is the async backbone; local development uses containerized Kafka; deployed environment uses managed Kafka.               | **SIGNED OFF** |
| **Kafka partitioning**      | Repository-related events partition by canonical_repository_key to preserve per-repository ordering.                            | **SIGNED OFF** |
| **Retrieval boundary**      | Separate logical capability but co-deployed inside agent-service for V1.                                                        | **SIGNED OFF** |
| **Retrieval style**         | Hybrid RAG: vector + keyword/BM25-like retrieval + fusion + reranking + citations.                                              | **SIGNED OFF** |
| **Progress delivery**       | REST for commands/queries/HITL, SSE for server-to-client progress, REST job-status endpoint for recovery.                       | **SIGNED OFF** |
| **Authentication**          | Public analysis is allowed; authentication becomes mandatory for stateful/private/write/governance capabilities.                | **SIGNED OFF** |
| **Authorization**           | Capability-specific authorization; admin-only canonical taxonomy/governance actions. Gateway is not the sole enforcement point. | **SIGNED OFF** |
| **Technology discovery**    | Owned by Graph Intelligence; discoveries become candidates, not immediate canonical truth.                                      | **SIGNED OFF** |
| **Role discovery**          | Contextual role classification/discovery owned by Graph Intelligence; role can vary by repository usage.                        | **SIGNED OFF** |
| **Canonical taxonomy**      | Owned exclusively by Governance; semantic promotion/merge/split/rename/deprecate is governed.                                   | **SIGNED OFF** |
| **Specialist agents**       | Architecture/risk/reliability/migration/verification remain internal agent-service components, not separate microservices.      | **SIGNED OFF** |
| **Flink**                   | Later streaming analytics workload; not a REST microservice and not required for V1.                                            | **LATER**      |
| **Spark**                   | Later offline/corpus-scale workload; not a REST microservice and not required for V1.                                           | **LATER**      |
| **vLLM**                    | Later model-serving service for open/fine-tuned models and fallback/model routing.                                              | **LATER**      |
| **Multi-region**            | Later; initial deployment is single region/multi-zone.                                                                          | **LATER**      |

# 2\. System Context and High-Level Boundaries

```
                              React Web App
                                    │
                               REST + SSE
                                    │
                                    ▼
                             gateway-service
                            Java / Spring Boot
                                    │
             ┌──────────────────────┼──────────────────────┐
             ▼                      ▼                      ▼
   repository-service       governance-service       agent-service
    Java / Spring            Java / Spring             Python
             │                      ▲                      │
             │                      │                      │
             └──────────────────── Kafka ──────────────────┘
                                    │
                            ┌───────┴────────┐
                            ▼                ▼
                          Flink            Spark
                          later            later

agent-service (V1 co-deployment)
     │
     ├── LangGraph agent runtime
     ├── Retrieval capability → Hybrid RAG / citations
     ├── MCP / repository tools
     ├── Graph Intelligence client → Neo4j / GraphRAG
     └── LLMClient → Gemini → vLLM [later]

Shared data/infrastructure
     MongoDB   Redis   Neo4j   GCS   Managed Kafka
```

**Core design principle:** each service owns its domain state and integrates through APIs/events. No service may use another service's database collections as its integration contract.

# 3\. Service / Workload Inventory

| **Service / workload**            | **Runtime**              | **Plane**       | **Purpose**                                                                                | **Release**     |
| --------------------------------- | ------------------------ | --------------- | ------------------------------------------------------------------------------------------ | --------------- |
| **web-app**                       | React                    | Presentation    | Repository submission/results, agent review, evidence, governance, eval/ops UI.            | V1 + extensions |
| **gateway-service**               | Spring Boot              | Platform        | External API boundary, auth context, rate limiting, routing, correlation.                  | V1              |
| **repository-service**            | Spring Boot              | Platform        | Repository lifecycle, ingestion, deterministic analysis, jobs, source/evidence extraction. | V1              |
| **governance-service**            | Spring Boot              | Governance      | Feedback, review/HITL, canonical taxonomy, candidate promotion, audit.                     | V1              |
| **agent-service**                 | Python/FastAPI/LangGraph | AI              | Architecture review, planning, tools, reasoning, verification, durable workflow.           | V1              |
| **retrieval capability**          | Python (co-deployed)     | AI/Data         | Evidence chunking/indexing, hybrid retrieval, reranking, citations.                        | V1              |
| **graph-intelligence-service**    | Python + Neo4j           | AI/Data         | Knowledge graph, GraphRAG, technology/role discovery, novelty, later GNN.                  | V1/V2           |
| **model-service**                 | Python + vLLM            | AI Infra        | Serve open/fine-tuned models; later model routing/fallback.                                | Later           |
| **stream-analytics**              | Flink                    | Data Processing | Real-time event/AgentOps/technology trend aggregations.                                    | Later           |
| **offline-analytics**             | Spark/PySpark            | Data Processing | Corpus backfills, datasets, graph features, eval/training jobs.                            | Later           |
| **training/evaluation workloads** | Python jobs              | ML Ops          | Golden/eval datasets, SFT/DPO/RLHF experiments, GNN/model training.                        | Later           |

# 4\. Web App

**React presentation layer • V1 + EXTENSIONS**

The browser application is the presentation surface for repository analysis, agent-driven architecture review, evidence inspection, governance, evaluations and operations views.

## Responsibilities

- Submit/normalize repository analysis requests and display streamed progress.
- Display stack, software type, roles/layers, evidence and similar repositories.
- Expose Agent Review workflow, architecture findings and Evidence Explorer.
- Expose review/HITL actions according to user permissions.
- Later expose technology ecosystem graph, architecture history, evaluations and AgentOps dashboards.

## Major components

```
web-app
├── Repository submission / results
├── Agent Review workspace
├── Architecture Findings
├── Evidence Explorer
├── Review / HITL
├── Evaluations
└── AgentOps / Graph views [later]
```

## Data / storage ownership

- No canonical business database ownership in the browser.
- Client-side transient state only; durable state remains server-side.

## Communication

- HTTPS REST for commands and queries.
- SSE for long-running job/agent progress.
- REST job-status endpoint is the reconnect/recovery mechanism.

## Cache / concurrency

- Browser may use normal short-lived client caching, but server-side cache policy is authoritative.

## Security boundary

- Public repository analysis may remain accessible without login.
- Authenticated/private/write/governance operations follow gateway and owning-service authorization.

## Scaling / availability

- Static web deployment/CDN as appropriate; does not scale with backend job execution.

## Explicitly does not own

- Repository analysis logic.
- Agent orchestration.
- Taxonomy or canonical governance state.
- Graph intelligence.

## Signed-off decisions

- SSE remains primary real-time progress mechanism; WebSocket deferred.
- Hidden model chain-of-thought is never displayed; only high-level execution/tool/evidence status.

# 5\. gateway-service

**Java / Spring Boot • Platform plane • V1 SIGNED OFF**

The external API boundary for the product. It provides cross-cutting request policies while delegating domain work to owning services.

## Responsibilities

- Route repository, agent and governance APIs.
- Apply authentication when required and propagate identity/claims.
- Apply coarse route authorization and rate limits.
- Create/propagate request and correlation identifiers.
- API versioning, request validation and health/routing behavior.

## Major components

```
gateway-service
├── Authentication
├── Authorization context
├── PublicRoutePolicy
├── RateLimitPolicy
├── RequestCorrelation
├── RepositoryApiFacade
├── AgentApiFacade
└── GovernanceApiFacade
```

## Data / storage ownership

- No domain source-of-truth data.
- Redis may hold rate-limit/ephemeral gateway state.

## Communication

- Inbound HTTPS REST/SSE from React.
- Synchronous REST to platform services as appropriate; long work becomes jobs/events.

## Cache / concurrency

- Redis for rate limiting and short-lived policy state.

## Security boundary

- Authentication is platform-wide infrastructure but not mandatory for all public analysis endpoints.
- Owning downstream service re-enforces sensitive permissions; gateway is not the sole security boundary.

## Scaling / availability

- Stateless horizontal scaling behind load balancer.

## Explicitly does not own

- Repository fetching/analysis.
- AI reasoning.
- Taxonomy promotion.
- Graph queries as a domain owner.

## Signed-off decisions

- Public analysis + restricted governance/write actions.
- Capability-level authorization rather than all-or-nothing login.

# 6\. repository-service

**Java / Spring Boot • Platform plane • V1 SIGNED OFF**

Authoritative service for repository identity, repository lifecycle, source acquisition, deterministic facts, analysis job ownership and deterministic evidence.

## Responsibilities

- Normalize GitHub/GitLab repository identity and resolve commit SHA.
- Fetch/clone repository into ephemeral working storage.
- Read file tree and parse manifests/dependencies.
- Run deterministic technology/file/config detection and artifact classification.
- Produce normalized repository snapshot and deterministic evidence.
- Coordinate analysis jobs, freshness and duplicate-request suppression.
- Publish repository lifecycle/analysis events.

## Major components

```
repository-service
├── Repository API
├── RepositoryIngestionService
├── RepositoryNormalizer
├── RepositoryFetcher
├── RepositoryTreeReader
├── ManifestParser
├── DependencyExtractor
├── DeterministicAnalyzer
├── EvidenceExtractor
├── AnalysisCoordinator
├── AnalysisCache
├── AnalysisJobStore
├── FreshnessPolicy
└── RepositoryEventPublisher
```

## Data / storage ownership

- MongoDB: repositories, versions, analysis jobs, deterministic/analysis results as applicable.
- Ephemeral disk: working repository clone.
- GCS: persisted derived/normalized artifacts; optional full source snapshot only when explicitly required.
- Canonical identity includes repository key, commit SHA and pipeline version.

## Communication

- REST from gateway for submit/query.
- Kafka for async analysis lifecycle and downstream consumers.
- May expose repository/tool APIs to agent-service rather than allowing DB access.

## Cache / concurrency

- Redis caches fresh analysis/metadata and accelerates request coalescing.
- Durable duplicate-work protection is based on unique analysis identity in MongoDB.
- Identical repo + SHA + pipelineVersion requests attach to the same logical job.
- Leases/heartbeats/reclaim behavior will be specified in LLD.

## Security boundary

- Public repository analysis can be public/rate-limited.
- Private repository access later requires authenticated credentials and stricter retention rules.

## Scaling / availability

- Stateless API/worker pods scale horizontally.
- Kafka buffers spikes/backpressure; workers scale based on queue lag and analysis latency.

## Explicitly does not own

- Canonical taxonomy.
- Architecture reasoning.
- Graph/role discovery.
- Final human approval.

## Signed-off decisions

- Repository clones are ephemeral by default.
- Persist derived artifacts; optional source archive in GCS.
- MongoDB remains operational truth.

# 7\. governance-service

**Java / Spring Boot • Governance plane • V1 SIGNED OFF**

The authority for human feedback, review/HITL, canonical technologies/roles/taxonomy, semantic promotion decisions and audit history.

## Responsibilities

- Capture feedback/corrections.
- Maintain review queue and approval/rejection workflow.
- Own canonical technology, role, software-type and layer taxonomies.
- Review technology/role candidates produced by Graph Intelligence.
- Support promote, merge, split, rename, move, deprecate and reject operations where applicable.
- Version taxonomy changes and preserve audit history.
- Authorize sensitive semantic/write actions.

## Major components

```
governance-service
├── Feedback
├── ReviewQueue
├── HumanReview
├── ApprovalPolicy
├── TechnologyRegistry
├── RoleRegistry
├── TaxonomyService
├── CandidatePromotion
├── TaxonomyVersioning
└── AuditService
```

## Data / storage ownership

- MongoDB: feedback, reviews, candidate review state, canonical taxonomy, taxonomy versions, audit events.
- Graph store may contain projections of canonical/candidate concepts but is not authority.

## Communication

- REST from gateway for feedback/review/admin operations.
- Kafka consumes discovery/review events and publishes taxonomy/governance events.
- Agent-service uses explicit HITL/governance APIs/events for approval-dependent actions.

## Cache / concurrency

- Small read caches are allowed; canonical decisions always read/write through durable governance state.

## Security boundary

- Admin/authorized maintainer required for canonical taxonomy mutation, promotion, retraining/config changes and other governed actions.
- Submitting feedback may remain public/authenticated based on abuse/rate policy.

## Scaling / availability

- Stateless service pods; MongoDB provides durable state.
- Review work is low-throughput compared with analysis but correctness/auditability is higher priority.

## Explicitly does not own

- Technology/role discovery algorithms.
- Repository analysis.
- LLM orchestration.

## Signed-off decisions

- AI may propose; Governance alone makes ontology changes canonical.
- Canonical semantic changes are human-governed.

# 8\. agent-service

**Python / FastAPI / LangGraph • AI plane • V1 SIGNED OFF**

Primary agentic intelligence service. It turns repository facts/evidence into goal-driven architecture investigation, risk analysis, verification and grounded recommendations.

## Responsibilities

- Create and persist agent runs/workflow state.
- Plan multi-step architecture review.
- Select and execute read-only tools.
- Use co-deployed retrieval for grounded evidence.
- Query Graph Intelligence / GraphRAG when needed.
- Invoke Gemini through LLM abstraction; later support vLLM/model routing.
- Perform architecture, reliability, risk, migration and verification capabilities.
- Enforce step/tool/time/token/cost budgets.
- Pause/resume for human approval when a controlled write is requested.

## Major components

```
agent-service deployment
├── FastAPI / Agent API
├── LangGraph AgentRuntime
├── Planner
├── Tool Execution
├── Architecture / Risk / Reliability / Migration capabilities
├── Verification
├── Budget / Policy
├── Checkpointing
├── Retrieval capability (co-deployed)
└── MCP / repository tools
```

## Data / storage ownership

- MongoDB: agent runs, durable checkpoints, findings, recommendations, tool/audit metadata as required.
- Redis: short-lived execution/result caches only.
- No direct read of repository-service/governance collections; use service APIs/tools/events.

## Communication

- REST from gateway for commands/status.
- SSE progress surfaces through API/gateway path.
- Repository tools/APIs for facts/source evidence.
- Graph Intelligence API for graph/GraphRAG.
- Governance API/events for HITL decisions.
- Kafka publishes/consumes agent lifecycle events.
- External LLM API: Gemini; vLLM later.

## Cache / concurrency

- Cache deterministic tool results/retrieval inputs more aggressively than final generative recommendations.
- Agent result identity includes repository SHA + review goal + agent/prompt/retrieval/model versions.
- Durable checkpointing prevents process death from losing accepted workflow state.

## Security boundary

- READ tools may execute automatically.
- WRITE tools require explicit authority/HITL; destructive operations remain disabled unless deliberately introduced.
- Repository content is untrusted input and must not be allowed to redefine tool policy.

## Scaling / availability

- Horizontally scale agent workers independently of repository service.
- Bound concurrency by queue/workflow/model limits; do not fan out unbounded specialist agents.

## Explicitly does not own

- Canonical taxonomy promotion.
- Repository identity/source ownership.
- Graph persistence ownership.

## Signed-off decisions

- LangGraph is production runtime.
- Specialist agents are internal components, not separate services.
- Retrieval is co-deployed for V1.

# 9\. Retrieval Capability

**Python • Co-deployed inside agent-service for V1 • V1 SIGNED OFF**

Provides evidence retrieval and grounding while maintaining a clean logical boundary so it can later be split into an independently scalable service without changing agent-domain code.

## Responsibilities

- Create evidence/code/document chunks from repository-derived content.
- Generate/use embeddings.
- Perform vector semantic retrieval.
- Perform keyword/BM25-like retrieval.
- Fuse candidate sets, rerank and produce ranked evidence.
- Create source/file/line citations and retrieval metadata.
- Support retrieval evaluation.

## Major components

```
retrieval capability
├── Evidence ingestion/chunking
├── Embedding adapter
├── Vector retriever
├── Keyword retriever
├── Hybrid fusion
├── Reranker
└── Citation / Evidence assembler
```

## Data / storage ownership

- Logical retrieval/index data owned by retrieval capability.
- Exact vector/search backend remains an implementation detail for LLD; do not add another database solely for résumé coverage.
- Large/derived artifacts may be persisted to GCS; operational metadata can remain in MongoDB.

## Communication

- In-process interface from agent-service in V1.
- If split later, same logical contract can become remote API.

## Cache / concurrency

- Embedding cache keyed by chunk/content hash + embedding model/version.
- Retrieval query caching is optional and version-aware.

## Security boundary

- Retrieval is read-oriented; it must respect repository access permissions for private repositories later.

## Scaling / availability

- Co-deployed initially. Split only when independent scaling, failure isolation, large index load or GPU reranking justifies it.

## Explicitly does not own

- Agent planning/policy.
- Canonical taxonomy.
- Graph discovery.

## Signed-off decisions

- Hybrid RAG is the target retrieval strategy.
- Physical microservice split deferred.

# 10\. graph-intelligence-service

**Python + Neo4j • AI/Data plane • SIGNED OFF LOGICAL SERVICE**

Owns relationship intelligence: knowledge graph, GraphRAG, technology/role discovery, novelty detection and later graph ML/GNN inference.

## Responsibilities

- Project canonical repository/technology/role/layer/pattern facts into Neo4j.
- Support graph queries and GraphRAG.
- Model contextual technology usage rather than one global role per technology.
- Discover unknown/new technologies and create TechnologyCandidates.
- Classify existing roles and discover RoleCandidates when taxonomy fit is poor.
- Deduplicate/cluster candidates and accumulate cross-repository evidence.
- Compute/cooperate on technology co-occurrence/graph neighborhoods.
- Later export graph datasets and serve advisory GNN predictions.

## Major components

```
graph-intelligence-service
├── GraphProjectionService
├── KnowledgeGraphService
├── GraphQueryService
├── GraphRAGRetriever
├── OntologyDiscovery
│   ├── TechnologyDiscovery
│   ├── RoleClassification
│   ├── RoleDiscovery
│   ├── CandidateResolver
│   └── NoveltyDetection
├── CoOccurrenceAnalyzer
└── GraphPredictor [GNN later]
```

## Data / storage ownership

- Neo4j: rebuildable knowledge-graph projection and relationship intelligence.
- MongoDB/governance state remains canonical for operational/candidate/taxonomy truth.
- Graph may contain candidate projections for discovery but not authoritative review state.

## Communication

- Consumes repository/taxonomy/discovery events through Kafka.
- APIs queried by agent-service for GraphRAG and graph intelligence.
- Publishes technology/role candidate events to Governance.

## Cache / concurrency

- May cache expensive graph queries/derived aggregates, but Neo4j remains the graph projection store.

## Security boundary

- Read/query access can be broad; taxonomy mutation cannot happen here.
- Candidate proposals do not bypass Governance.

## Scaling / availability

- Independent service because graph workload, indexes and future GNN/GraphRAG scaling differ from agent orchestration.
- Neo4j is rebuildable if projection is lost/corrupt.

## Explicitly does not own

- Canonical taxonomy approval.
- Repository job lifecycle.
- Agent workflow state.

## Signed-off decisions

- Neo4j selected.
- Technology/role discovery is open-world candidate discovery + governed promotion.
- Role is contextual to TechnologyUsage within a repository.

# 11\. model-service

**Python + vLLM • AI infrastructure • LATER**

Future serving boundary for open/fine-tuned models once StackSniffer has a bounded task and measured evidence that a local/tuned model adds value.

## Responsibilities

- Serve open models through vLLM.
- Serve LoRA/fine-tuned adapters.
- Expose health/inference metrics.
- Support later model routing/fallback from agent-service.

## Major components

```
model-service
├── vLLM
├── Model Registry
├── Adapter Registry
└── Inference API
```

## Data / storage ownership

- GCS: model/adaptor artifacts.
- MongoDB: model metadata/version/eval metadata as needed.

## Communication

- HTTP/OpenAI-compatible inference from AI plane.

## Cache / concurrency

- Inference cache only if proven useful; model correctness/versioning must stay explicit.

## Security boundary

- Model access/service auth when deployed; no governance bypass.

## Scaling / availability

- Independent GPU/model scaling later.

## Explicitly does not own

- Training datasets.
- Agent planning.
- Canonical model promotion policy if governance expands later.

## Signed-off decisions

- Not a V1 dependency.
- Gemini remains primary initially.

# 12\. stream-analytics

**Apache Flink • Data-processing workload • LATER**

Future continuous processing of Kafka events for online technology, discovery and AgentOps aggregates. This is a streaming workload, not a REST microservice.

## Responsibilities

- Compute technology usage/co-occurrence trends.
- Compute agent failure/fallback/approval/cost/latency windows.
- Support emerging-technology signals and dashboards.
- Persist materialized aggregates for query consumers.

## Major components

```
Flink jobs
├── TechnologyUsage
├── CoOccurrence
├── EmergingTechnology
├── AgentFailure / Fallback
└── Cost / Latency / Approval Trends
```

## Data / storage ownership

- Consumes Kafka; writes analytics/materialized outputs to chosen operational/analytics stores. Exact sink is LLD/later.

## Communication

- Kafka input; downstream aggregates queried through owning services/UI, not direct browser access.

## Cache / concurrency

- Checkpoint/state caching is Flink-managed; operational caches remain separate.

## Security boundary

- No user-facing authorization surface by default; protected infrastructure workload.

## Scaling / availability

- Scale by partitions/parallelism; checkpoint/recovery semantics are required when introduced.

## Explicitly does not own

- Repository analysis.
- Offline corpus backfills.

## Signed-off decisions

- Later only; distinct from Spark.

# 13\. offline-analytics

**Spark / PySpark • Data-processing workload • LATER**

Future offline/corpus-scale computation for datasets, graph features, embedding backfills, hard-example mining and historical evaluation.

## Responsibilities

- Build evaluation/training/preference datasets.
- Compute graph/GNN features.
- Perform embedding/backfill jobs.
- Join historical traces/feedback for offline analytics.
- Generate versioned corpus artifacts.

## Major components

```
Spark jobs
├── Eval Dataset Builder
├── Training / Preference Dataset Builder
├── Graph Feature Builder
├── Embedding Backfill
└── Historical / Hard-example jobs
```

## Data / storage ownership

- Reads MongoDB/GCS/event history as appropriate; writes versioned datasets/artifacts to GCS.

## Communication

- Batch/job orchestration rather than request/response API.

## Cache / concurrency

- Not a cache workload.

## Security boundary

- Infrastructure/service identity controls access to production datasets.

## Scaling / availability

- Scale per batch job; not always running.

## Explicitly does not own

- Real-time streaming.
- Online agent orchestration.

## Signed-off decisions

- Later only; offline role kept distinct from Flink.

# 14\. Training & Evaluation Workloads

**Python ML jobs • ML Ops • LATER / RESEARCH**

Job-oriented capabilities for evaluation, fine-tuning, preference optimization and later GNN/RL experiments. These are not required to be permanent REST microservices.

## Responsibilities

- Run golden/deterministic and semantic evaluation suites.
- Create/consume approved feedback datasets.
- Run SFT/LoRA/QLoRA experiments.
- Build preference datasets and DPO experiments.
- Later run reward-model/PPO/GRPO research only if justified.
- Train/evaluate GNN models.

## Major components

```
ML workloads
├── EvalRunner
├── DatasetBuilder
├── FineTuningJob
├── DPOJob
├── Reward/RL experiments [later]
└── GNNTrainingJob
```

## Data / storage ownership

- GCS: versioned eval/training/preference/model artifacts.
- MongoDB: experiment/model/eval metadata as needed.

## Communication

- Triggered as jobs from CI/admin/orchestration. Results are consumed by evaluation/admin surfaces and model registry.

## Cache / concurrency

- No special caching requirement beyond reusable datasets/features.

## Security boundary

- Only approved/high-confidence labels enter supervised training truth. AI predictions cannot automatically train on themselves.

## Scaling / availability

- Ephemeral jobs; GPU/compute scale on demand.

## Explicitly does not own

- Production agent runtime.
- Canonical governance decisions.

## Signed-off decisions

- Current feedback is not called true RLHF. DPO comes before reward-model + policy optimization research.

# 15\. Shared Storage and Infrastructure Components

| **Component**            | **Architectural role**                                                                                                                        | **Primary content**                                                                                                                   |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| **MongoDB**              | Primary operational/source-of-truth database. Existing base is retained. Service-owned collections/databases may share one cluster initially. | Repositories, versions, jobs, analyses, feedback, governance/taxonomy state, candidates, agent runs/checkpoints/findings.             |
| **Redis**                | Fast ephemeral infrastructure; never canonical.                                                                                               | Analysis/result caches, rate limits, request coalescing, short-lived coordination and selected agent/tool caches.                     |
| **Neo4j**                | Dedicated graph/intelligence projection, selected and signed off. Rebuildable from canonical facts/events.                                    | Repository/technology/role/layer/pattern relationships, TechnologyUsage, GraphRAG, discovery neighborhoods, later GNN dataset source. |
| **Google Cloud Storage** | Large immutable/versioned artifact store.                                                                                                     | Derived repository artifacts, optional source snapshots, datasets, evaluation outputs, model/adaptor artifacts.                       |
| **Kafka**                | Asynchronous event backbone and backpressure buffer.                                                                                          | Repository, agent, discovery, governance and learning lifecycle events; local containerized Kafka, managed Kafka when deployed.       |

# 16\. Cross-Cutting HLD Decisions

## 16.1 Service data ownership

- Each service owns its domain state.
- No cross-service Mongo collection queries as an integration mechanism.
- Integration occurs through APIs/tools/events.
- Neo4j is projection/intelligence, not a second source of truth.

## 16.2 Cache and concurrency

- Repository analysis identity = canonical repository key + commit SHA + pipeline version.
- Identical concurrent requests are coalesced into one logical analysis job.
- MongoDB supplies durable/atomic job ownership; Redis accelerates lookup/coordination.
- Kafka buffers spikes and creates backpressure between request acceptance and worker throughput.
- Final generative outputs are cached conservatively compared with deterministic facts/evidence/tool results.

## 16.3 Repository retention

- Clone to ephemeral workspace for analysis.
- Persist normalized/derived evidence/artifacts and version metadata.
- GCS full source archive is optional, not default.
- Private repositories later default to minimal retention unless explicitly configured.

## 16.4 Security and HITL

- Public analysis can remain available/rate limited.
- Admin or authorized maintainer required for canonical taxonomy changes and other governed actions.
- READ tools may run automatically; WRITE actions require explicit human authorization; destructive tools remain disabled until explicitly designed.
- Gateway propagates identity, but owning service enforces sensitive permission.

## 16.5 AI/ontology learning

- Technology and role taxonomies are open-world: unknown usage becomes a candidate instead of being silently dropped or immediately canonized.
- Graph Intelligence accumulates evidence/statistics and proposes discoveries.
- Governance performs semantic promotion/merge/split/rename/deprecate decisions.
- Only approved/high-confidence truth enters supervised training datasets.

## 16.6 Real-time client communication

- REST = commands, queries and HITL decisions.
- SSE = long-running analysis/agent progress.
- REST job-status = reconnect/recovery fallback.
- WebSocket deferred until a genuinely bidirectional persistent interaction requires it.

## 16.7 Deployment and availability

- Initial deployment is single region, multiple availability zones where managed dependencies support it.
- Kubernetes hosts stateless/API/worker workloads; managed stateful infrastructure is preferred.
- Services scale horizontally and independently.
- Multi-region remains a later architecture exercise.

# 17\. Approved Core HLD Flows

## 17.1 Repository analysis

```
User → React → Gateway → repository-service
                         │
                         ├─ resolve repo / SHA
                         ├─ cache + active-job check
                         ├─ ephemeral clone
                         ├─ deterministic analysis / evidence
                         ├─ MongoDB durable result/job state
                         ├─ GCS derived artifacts [as needed]
                         └─ Kafka: repository.analysis.completed
                                      │
                         ┌────────────┴────────────┐
                         ▼                         ▼
                graph-intelligence           other consumers
```

## 17.2 Agent architecture review

```
User → Gateway → agent-service / LangGraph
                     │
                     ├─ repository tools/facts
                     ├─ co-deployed Hybrid RAG
                     ├─ GraphRAG / Neo4j
                     ├─ Gemini (vLLM later)
                     └─ verification
                              │
                              ▼
                     evidence-backed findings
                              │
                 [write requested?] ── yes ──→ Governance / HITL
```

## 17.3 Technology / role discovery

```
Repository evidence
      │
      ▼
Known technology / contextual role fit?
      │
  ┌───┴───────────────┐
  │                   │
 known              unknown / poor fit
  │                   │
  ▼                   ▼
assign usage     TechnologyCandidate / RoleCandidate
                          │
                   graph/corpus evidence
                          │
                          ▼
                       emerging
                          │
                          ▼
                     Governance
                     /   |   \
                promote merge reject
                          │
                          ▼
                  canonical taxonomy
```

# 18\. Explicitly Deferred to LLD

- Exact REST endpoints, request/response DTOs and validation contracts.
- Kafka topic naming finalization, event payload schemas, headers and schema evolution strategy.
- MongoDB collection/document/index definitions.
- Neo4j node labels, relationship properties, constraints and Cypher query contracts.
- LangGraph AgentState, node/edge definitions, checkpoint representation and tool-call schemas.
- Python/Java interfaces, class diagrams, IoC composition roots and dependency implementation details.
- Cache-key exact formats, TTLs, lease/heartbeat timing, retry counts and idempotency keys.
- SSE event payload schema and frontend recovery mechanics.
- Concrete IAM roles/service accounts/secrets layout.
- Kubernetes manifests, resource requests/limits, HPA policy and managed-service SKUs.

# 19\. HLD Sign-Off Statement

**SIGNED OFF:** The service/component boundaries and HLD decisions captured in this document are approved as the working StackSniffer architecture. The next design activity is Low-Level Design. Any material change to these boundaries should be recorded as an HLD/ADR change before stories or implementation are based on it.

# 20\. Capability Ownership Matrix

| **Capability**                                        | **Owner**                                        |
| ----------------------------------------------------- | ------------------------------------------------ |
| **Public API / routing**                              | gateway-service                                  |
| **Repository identity / source lifecycle**            | repository-service                               |
| **Deterministic technology/evidence detection**       | repository-service                               |
| **Analysis job coordination / duplicate suppression** | repository-service                               |
| **Architecture agent orchestration**                  | agent-service                                    |
| **Hybrid RAG / reranking / citations**                | agent-service retrieval capability (co-deployed) |
| **Knowledge graph / GraphRAG**                        | graph-intelligence-service                       |
| **Technology discovery**                              | graph-intelligence-service                       |
| **Role classification / discovery**                   | graph-intelligence-service                       |
| **Canonical taxonomy / promotion**                    | governance-service                               |
| **Feedback / review / HITL / audit**                  | governance-service                               |
| **Operational source of truth**                       | MongoDB                                          |
| **Cache / short-lived coordination**                  | Redis                                            |
| **Graph projection**                                  | Neo4j                                            |
| **Large/versioned artifacts**                         | GCS                                              |
| **Async events / backpressure**                       | Kafka                                            |
| **Streaming analytics**                               | Flink \[later\]                                  |
| **Offline corpus processing**                         | Spark \[later\]                                  |
| **Open model serving**                                | vLLM model-service \[later\]                     |
| **Evaluation / post-training / GNN training**         | Job workloads \[later\]                          |

# 21\. Signed-Off Communication Architecture

**Purpose:** make the integration contract between the approved services explicit at HLD level. The exact endpoints, DTOs, Kafka payloads, retry codes and timeout values remain LLD concerns.

## 21.1 Communication principles

- Browser-to-platform communication uses HTTPS. REST carries commands, queries and human decisions; SSE carries long-running progress.
- Synchronous service-to-service calls are used when the caller needs an immediate answer to continue the current user request.
- Kafka is used for asynchronous lifecycle events, fan-out, buffering, replay and work that should not couple the caller to downstream processing time.
- Retrieval is a separate logical component but is co-deployed with agent-service for V1, so its V1 call path is in-process rather than network-based.
- No service directly queries another service's MongoDB collections or Neo4j data as an integration contract.
- MongoDB is operational truth; Redis is ephemeral cache/coordination; Neo4j is a rebuildable intelligence projection; GCS stores durable artifacts rather than service workflow truth.
- Every cross-service request/event propagates correlation identifiers such as repository key, analysis/job ID and agent-run ID where applicable.

## 21.2 Service-to-service communication matrix

| **Caller / Producer**          | **Target / Consumer**         | **Mode**               | **Protocol**                         | **Purpose**                                                        | **HLD rule**                                                                |
| ------------------------------ | ----------------------------- | ---------------------- | ------------------------------------ | ------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| **React Web App**              | gateway-service               | Sync + stream          | HTTPS REST + SSE                     | Submit/query work; receive progress; send HITL decisions           | Browser communicates only through gateway-facing APIs.                      |
| **gateway-service**            | repository-service            | Sync                   | HTTPS REST                           | Submit repository analysis; query repository/analysis status       | Expensive work becomes a durable job rather than a long blocking request.   |
| **gateway-service**            | agent-service                 | Sync + stream          | HTTPS REST + SSE path                | Start/query architecture reviews; stream run progress              | Gateway routes; agent owns workflow state.                                  |
| **gateway-service**            | governance-service            | Sync                   | HTTPS REST                           | Feedback, review, approval/rejection, taxonomy/admin actions       | Sensitive authorization rechecked by Governance.                            |
| **repository-service**         | Kafka                         | Async publish          | Kafka                                | Repository and analysis lifecycle events                           | Repository key is the primary ordering/partitioning key for repo lifecycle. |
| **repository-service**         | MongoDB                       | Data access            | Mongo driver                         | Repository/version/job/analysis operational state                  | Repository service owns its collections.                                    |
| **repository-service**         | Redis                         | Data access            | Redis protocol                       | Fresh-result cache, metadata cache, single-flight acceleration     | Redis never replaces durable job ownership.                                 |
| **repository-service**         | GCS                           | Artifact I/O           | GCS API                              | Persist derived repository artifacts; optional full snapshot       | Working clone is ephemeral by default.                                      |
| **agent-service**              | repository-service            | Sync                   | Internal service API / tool contract | Read repository facts/evidence/source context                      | Agent never reads repository collections directly.                          |
| **agent-service**              | retrieval capability          | In-process V1          | Python interface                     | Vector/keyword retrieval, fusion, rerank, citations                | Logical boundary remains separable despite co-deployment.                   |
| **agent-service**              | graph-intelligence-service    | Sync                   | Service API                          | GraphRAG, relationship queries, discovery context                  | Graph service owns Neo4j access.                                            |
| **agent-service**              | governance-service            | Sync + async           | REST + Kafka                         | Pause/resume HITL; obtain/record governed decisions                | Write actions cannot bypass Governance.                                     |
| **agent-service**              | Gemini                        | Sync external          | HTTPS API                            | LLM reasoning / structured generation                              | Called through LLM abstraction; vLLM later.                                 |
| **agent-service**              | Kafka                         | Async pub/sub          | Kafka                                | Agent lifecycle, findings and downstream processing                | Async events must not become the source of workflow truth.                  |
| **agent-service**              | MongoDB                       | Data access            | Mongo driver                         | Agent runs, checkpoints, findings, recommendations                 | Durable state survives pod/process failure.                                 |
| **agent-service**              | Redis                         | Data access            | Redis protocol                       | Short-lived tool/retrieval/result caches                           | Final generative outputs cached conservatively.                             |
| **graph-intelligence-service** | Kafka                         | Async pub/sub          | Kafka                                | Consume repository/taxonomy facts; publish discovery candidates    | Graph projection updates are event-driven/rebuildable.                      |
| **graph-intelligence-service** | Neo4j                         | Data access            | Neo4j driver                         | Knowledge graph, GraphRAG and relationship queries                 | Neo4j is not canonical operational truth.                                   |
| **graph-intelligence-service** | governance-service            | Async + sync as needed | Kafka + service API                  | Submit technology/role candidates; read canonical taxonomy context | Discovery proposes; Governance canonizes.                                   |
| **governance-service**         | MongoDB                       | Data access            | Mongo driver                         | Feedback, review, taxonomy, versions, audit state                  | Canonical ontology/governance truth lives here.                             |
| **governance-service**         | Kafka                         | Async pub/sub          | Kafka                                | Publish approved/rejected/taxonomy-change events                   | Downstream projections react to canonical changes.                          |
| **Flink \[later\]**            | Kafka                         | Async consume          | Kafka                                | Real-time technology/AgentOps aggregates                           | Streaming workload, not request/response service.                           |
| **Spark \[later\]**            | MongoDB / GCS / event history | Batch                  | Batch connectors/APIs                | Offline datasets, features, backfills, historical evaluation       | Batch workload; outputs versioned artifacts to GCS.                         |

## 21.3 Communication topology

```
React
  │ REST / SSE
  ▼
Gateway
  ├──────── REST ───────► Repository Service
  ├──────── REST ───────► Governance Service
  └──────── REST/SSE ───► Agent Service
                              │
                              ├── in-process ─► Retrieval
                              ├── REST/API ───► Graph Intelligence ─► Neo4j
                              ├── REST/API ───► Repository Service
                              ├── REST/API ───► Governance / HITL
                              └── HTTPS ──────► Gemini

Repository / Agent / Governance / Graph Intelligence
                         │
                         └──────── Kafka ─────► async consumers

Operational state  ─► MongoDB
Cache/coordination ─► Redis
Artifacts          ─► GCS
Graph projection   ─► Neo4j
```

# 22\. Required Architecture Diagram Set

The following diagrams are part of the architecture documentation set. HLD diagrams explain boundaries and flows; LLD diagrams later explain concrete implementation structure.

## 22.1 HLD diagrams required before implementation

| **Diagram**                                        | **Purpose**                                                             | **Primary content**                                                                                      | **Status**   |
| -------------------------------------------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------------ |
| **Actor / Use-Case Diagram**                       | Show who interacts with StackSniffer and at what authority level.       | Public user, authenticated user, maintainer/admin, GitHub/GitLab, external LLM/model provider.           | **REQUIRED** |
| **System Context Diagram**                         | Show StackSniffer as a system and external dependencies.                | Users, source control, Gemini, managed Kafka, MongoDB/Redis/Neo4j/GCS boundaries.                        | **REQUIRED** |
| **Service / Container Diagram**                    | Show deployable services and logical workloads.                         | Gateway, Repository, Governance, Agent+Retrieval, Graph Intelligence, Flink/Spark/model-service later.   | **REQUIRED** |
| **Service Communication Diagram**                  | Show sync vs async integration paths.                                   | REST/SSE, Kafka, in-process retrieval, service APIs, external AI calls.                                  | **REQUIRED** |
| **Storage / Data Ownership Diagram**               | Show which service owns which persistence boundary.                     | Mongo operational ownership, Redis cache, Neo4j projection, GCS artifacts.                               | **REQUIRED** |
| **Kafka / Event Flow Diagram**                     | Show producers, event backbone and consumers.                           | Repository/agent/governance/graph events, later Flink/Spark consumers.                                   | **REQUIRED** |
| **Deployment Diagram**                             | Show runtime placement and managed dependencies.                        | Load balancer, Kubernetes/GKE workloads, managed Mongo/Redis/Kafka/Neo4j/GCS/external Gemini.            | **REQUIRED** |
| **Security / Trust-Boundary Diagram**              | Show trust zones and protected operations.                              | Public vs authenticated/admin routes, service identity, external repo/model boundaries, HITL write gate. | **REQUIRED** |
| **Repository Analysis Job State Diagram**          | Show durable high-level job lifecycle.                                  | QUEUED, RUNNING, COMPLETE, FAILED, recover/retry conceptual states.                                      | **REQUIRED** |
| **Sequence: New Repository**                       | Show first-time repository analysis across services.                    | Scenario 1 in this document.                                                                             | **REQUIRED** |
| **Sequence: Existing Repository / No Change**      | Show freshness/cache path with no expensive rerun.                      | Scenario 2 in this document.                                                                             | **REQUIRED** |
| **Sequence: Existing Repository / Feature Change** | Show new SHA with code/feature changes but stable technology set.       | Scenario 3 in this document.                                                                             | **REQUIRED** |
| **Sequence: Technology Migration**                 | Show technology additions/removals and graph/taxonomy effects.          | Scenario 4 in this document.                                                                             | **REQUIRED** |
| **Technology & Role Discovery Flow**               | Show candidate discovery, evidence accumulation and governed promotion. | Repository evidence → Graph Intelligence → candidate → Governance.                                       | **REQUIRED** |

## 22.2 Diagrams intentionally deferred to LLD

- Class/component diagrams inside each Spring/Python service.
- Exact LangGraph node/state transition diagram.
- Exact REST API sequence diagrams including DTO names/status codes.
- Kafka event-schema diagrams and schema-evolution/versioning details.
- MongoDB collection/document/index diagrams.
- Neo4j label/relationship/property schema diagram and Cypher query contracts.
- Cache key, lease/heartbeat and idempotency sequence diagrams.
- Detailed Kubernetes pod/HPA/resource topology.

## 22.3 Actor model for the HLD

```
Actors

Public User
  ├─ analyze public repository
  ├─ view results
  └─ submit permitted feedback

Authenticated User [future/expanded]
  ├─ saved/private repository workflows
  ├─ persistent personal run/history
  └─ approved external write workflow

Maintainer / Admin
  ├─ review corrections
  ├─ approve/reject technology & role candidates
  ├─ modify/version canonical taxonomy
  └─ controlled model/admin operations

External Systems
  ├─ GitHub / GitLab          source repository provider
  ├─ Gemini                   primary LLM provider
  ├─ Managed Kafka            asynchronous event backbone
  └─ MongoDB / Redis / Neo4j / GCS   managed data infrastructure
```

# 23\. Repository Lifecycle — Simple End-to-End Flows

**HLD rule:** repository freshness is determined primarily by canonical repository identity + commit SHA + pipeline version. Exact diff algorithms, changed-file invalidation and event payload names are deferred to LLD.

## 23.1 New repository

A repository has never been analyzed by StackSniffer, so no matching repository/version/analysis exists.

```
1. User → React → Gateway
       Submit repository URL.

2. Gateway → Repository Service
       Request analysis.

3. Repository Service
       Normalize repository identity.
       Resolve current commit SHA.
       Check Mongo/Redis → no existing analysis.

4. Repository Service
       Create durable analysis job.
       Clone repository to ephemeral workspace.
       Parse files/manifests/dependencies.
       Run deterministic technology/evidence discovery.

5. Repository Service
       Persist repository/version/analysis in MongoDB.
       Persist required derived artifacts to GCS.
       Publish analysis-completed facts/events to Kafka.

6. Retrieval (inside Agent Service)
       Index evidence/chunks for future grounded queries.

7. Graph Intelligence
       Consume/project repository ↔ technology/role/layer relationships into Neo4j.
       Create discovery candidates only when unknown technology/role evidence warrants it.

8. Agent / AI enrichment
       Use repository facts + retrieval + graph context + Gemini as required.

9. Result persisted → SSE COMPLETE → User sees result.
```

Expected outcome:

- One new repository/version analysis becomes operational truth in MongoDB.
- Derived artifacts are retained in GCS according to the signed-off retention policy.
- Neo4j receives a rebuildable relationship projection.
- Duplicate concurrent callers attach to the same logical analysis job.

## 23.2 Existing repository with no change

The repository is already known and the resolved commit SHA and StackSniffer pipeline version match a completed analysis.

```
1. User → Gateway → Repository Service
       Request analysis/result.

2. Repository Service
       Resolve canonical repository + current commit SHA.

3. Freshness check
       current SHA == analyzed SHA
       AND current pipeline version == analyzed pipeline version

4. Redis cache
       HIT → return cached result
       OR
       MISS → read completed result from MongoDB and repopulate Redis.

5. No repository clone.
   No deterministic re-analysis.
   No duplicate Gemini call.
   No graph rebuild for unchanged facts.

6. Existing result/status returned to UI.
       SSE is unnecessary for an already-complete immediate result unless UI chooses a uniform flow.
```

Expected outcome:

- Fast return path with no expensive repository or AI work.
- MongoDB remains authoritative if Redis is empty.
- If the pipeline version changed even though source SHA did not, the request is treated as stale and a new analysis may be scheduled.

## 23.3 Existing repository with feature/code changes but no technology migration

The repository exists, but a new commit SHA contains feature/behavior changes while the effective technology set remains unchanged.

```
1. User → Gateway → Repository Service
       Request/update analysis.

2. Repository Service
       Resolve current SHA.
       Compare with most recently analyzed SHA → DIFFERENT.

3. Repository Service
       Create a new versioned analysis job.
       Obtain changed repository content / changed-file context.
       Re-run deterministic analysis for the new snapshot.
       Preserve prior analysis as history.

4. Technology comparison
       Previous technology set == current effective technology set.
       Therefore this is a repository-version / feature change,
       not a technology migration.

5. Retrieval
       Refresh/re-index changed evidence/chunks.
       Reuse unchanged derived data where safe.

6. Graph Intelligence
       Update the repository-version/evidence projection as needed.
       Existing technology usage relationships remain valid unless new evidence changes role/layer context.

7. Agent / architecture analysis
       Re-evaluate only the analysis/review outputs affected by changed evidence where possible.
       Record architecture/history delta.

8. Persist new analysis/version → UI receives updated result.
```

Expected outcome:

- The old SHA remains historically addressable; the new SHA gets its own versioned analysis.
- Technology identities are not artificially removed/re-added when only application behavior changed.
- Incremental changed-file/chunk reuse is the HLD intent; exact invalidation algorithm is an LLD decision.

## 23.4 Existing repository with technology migration — addition and/or deletion

A new repository version changes its technology usage: a technology is added, removed, replaced, or its contextual role changes.

```
Example:
Spring Boot + RabbitMQ
        ↓ migration
Spring Boot + Kafka

1. User / repository refresh → Repository Service
       Resolve a new SHA and create a new versioned analysis.

2. Deterministic analysis
       Parse new manifests/config/source evidence.

3. Compare technology usage with previous analyzed version
       RabbitMQ: removed
       Kafka: added
       Spring Boot: retained

4. Persist new versioned analysis in MongoDB.
       Previous version is retained for architecture history.

5. Publish technology-change facts through Kafka
       (exact event names/payloads deferred to LLD).

6. Retrieval
       Remove/invalidate obsolete evidence for the new SHA.
       Index new Kafka evidence/config/code chunks.

7. Graph Intelligence / Neo4j
       Close/update the previous repository-version usage projection.
       Add new TechnologyUsage relationships for Kafka.
       Preserve historical RabbitMQ usage through the prior repository version.
       Recompute relevant graph/co-occurrence/discovery signals.

8. Role Discovery
       Classify Kafka's contextual role.
       If a new/poorly represented role is observed, create/accumulate a RoleCandidate.
       Unknown technology similarly becomes a TechnologyCandidate.

9. Governance
       Only receives a promotion review when candidate evidence merits governance.
       Normal known-technology migration needs no taxonomy approval.

10. Agent / architecture history
       Explain the migration using source evidence:
       what was added, removed, retained and what architecture implications changed.

11. New result returned to UI.
```

Expected outcome:

- Technology history is version-aware: deletion from the latest version does not erase historical use.
- Neo4j reflects current and historical repository-version relationships without becoming canonical operational truth.
- Known technology additions/removals update architecture intelligence automatically; only genuinely new ontology concepts enter governance review.
- The eventual Architecture History UI can explain migrations such as RabbitMQ → Kafka from evidence rather than comparing only labels.

## 23.5 Scenario decision table

| **Scenario**                        | **SHA**     | **Pipeline** | **Re-analyze?**                       | **Graph impact**                                     | **Primary fast/slow path**                |
| ----------------------------------- | ----------- | ------------ | ------------------------------------- | ---------------------------------------------------- | ----------------------------------------- |
| **New repository**                  | New         | Current      | Yes                                   | Create projection                                    | Slow/full analysis                        |
| **Existing / no change**            | Same        | Same         | No                                    | None                                                 | Fast/cache or Mongo result                |
| **Existing / feature change**       | New         | Current      | Yes; incremental optimization allowed | Update version/evidence; tech links may remain       | Versioned analysis                        |
| **Existing / technology migration** | New         | Current      | Yes                                   | Update TechnologyUsage + history + discovery signals | Versioned analysis + migration processing |
| **Existing / pipeline changed**     | Same or new | New          | Yes as required                       | Reproject if semantic output changed                 | Re-analysis due to analyzer version       |

# 24\. HLD Sign-Off Addendum

**SIGNED OFF ADDENDUM: The HLD now explicitly includes service/component communication modes, the required HLD diagram set, and the four repository lifecycle behaviors: new repository; existing repository with no change; existing repository with feature/code changes but stable technology usage; and existing repository with technology addition/removal/migration. Exact APIs, event schemas, diff algorithms, data models and state-machine implementation remain deferred to LLD.**

# 25\. Zero-Cost Infrastructure and Deployment Profiles — Signed-Off Addendum

**Final HLD constraint:** the complete V1 logical architecture must be runnable for development and portfolio demonstration without requiring a recurring paid managed-infrastructure bill. Production managed services remain the target topology, but are not prerequisites for implementing or demonstrating the system.

## 25.1 Deployment profiles

| **Capability**           | **LOCAL / PORTFOLIO — \$0 default**                                   | **FREE-CLOUD — best effort**                                             | **PRODUCTION TARGET**        |
| ------------------------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------ | ---------------------------- |
| **Application services** | Docker Compose/local containers                                       | Scale-to-zero/free-tier compute where available                          | Kubernetes/GKE or equivalent |
| **Operational DB**       | Local MongoDB                                                         | Free-tier managed Mongo option where available                           | Managed MongoDB              |
| **Cache**                | Local Redis                                                           | Free-tier/serverless Redis where available                               | Managed Redis                |
| **Graph**                | Neo4j Community local                                                 | Free-tier hosted Neo4j where available                                   | Managed Neo4j                |
| **Event backbone**       | Local Apache Kafka                                                    | Kafka if free; optional EventBus adapter when public hosting requires it | Managed Kafka                |
| **Artifacts**            | Local filesystem/MinIO                                                | GCS/free allowance where available                                       | GCS                          |
| **LLM**                  | Fake/mock for tests; external model only within acceptable free quota | Free quota where allowed                                                 | Paid Gemini / routed models  |
| **Later compute**        | Flink/Spark/vLLM/RLHF not continuously deployed                       | Not required for V1 demo                                                 | Dedicated later workloads    |

## 25.2 Architectural rule: logical service boundaries do not change by profile

```
RepositoryProvider / EventBus / ArtifactStore / CacheStore / GraphStore / LLMClient
        |
        +-- LOCAL adapters: Kafka, MinIO/local, Redis, Neo4j Community, local Mongo, Fake/Gemini
        +-- FREE-CLOUD adapters: best-effort free-tier providers where available
        +-- PROD adapters: managed Kafka, GCS, managed Redis/Mongo/Neo4j, paid model routing

Service ownership, events, data contracts, idempotency and HLD flows remain the same.
```

## 25.3 Cost-control decisions

- LOCAL / PORTFOLIO is the guaranteed no-budget path and is the default development profile.
- FREE-CLOUD is best-effort only; free-tier limits and provider pricing may change, so it is not treated as a correctness dependency.
- No eager corpus-wide reclassification is required when classifier/taxonomy contracts change; existing analyses refresh lazily when accessed. This avoids an uncontrolled compute bill.
- Private/proprietary repository content is not sent to third-party free model APIs by default.
- Flink, Spark, vLLM and RLHF/post-training are LATER on-demand workloads, not continuously running V1 infrastructure.
- Production managed-service deployment can be adopted later without redesigning domain services because infrastructure is inverted behind ports/adapters.

## 25.4 HLD sign-off impact

**SIGNED OFF: The zero-cost deployment requirement changes physical deployment choices, not the previously signed-off bounded contexts, data ownership, REST/SSE/Kafka communication model, Mongo/Redis/Neo4j/GCS logical roles, or the V1 service architecture.**