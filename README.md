# StackSniffer

Tech stack detection, domain analysis, and repo-aware AI chat engine.

StackSniffer analyzes GitHub repositories and returns structured analysis via REST API.
It is a detection service — it does NOT generate READMEs or write documentation.
Downstream consumers (such as genREADME) call `/api/analyze` to get results.

## Architecture

```
/
├── frontend/          React + Vite + TailwindCSS (port 5173)
│   ├── src/
│   │   ├── components/  UI components (chat, stack, insights, etc.)
│   │   ├── pages/       HomePage, ResultsPage
│   │   └── data/        Demo data
│   ├── index.html
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   └── package.json
└── backend/           Python FastAPI (port 8000)
    ├── routers/
    │   ├── analyze.py       Analysis + explainability endpoints
    │   └── chat.py          Repo-aware streaming chat endpoints
    ├── services/
    │   ├── stack_detector.py   Pass 1: deterministic pattern matching
    │   ├── ai_pipeline.py      Pass 2: Claude classification + insights
    │   ├── chat_service.py     Streaming chat grounded in StackAnalysis
    │   ├── github_service.py   GitHub API fetcher
    │   └── storage_service.py  MongoDB + in-memory fallback
    ├── models/
    │   └── schemas.py       Pydantic models for all request/response types
    └── config/
        └── patterns.json    Pattern matching rules by category
```

### Three-Layer Pipeline

1. **PASS 1 — `stack_detector.py`**: Deterministic pattern matching. Runs first, always. Zero API cost.
2. **PASS 2 — `ai_pipeline.py`**: Claude-powered classification and explainability. Runs second, always.
3. **CHAT — `chat_service.py`**: Repo-aware conversational agent grounded in the StackAnalysis. Streams responses in real time.

Both detection passes always run. AI is not optional or conditional.

## Setup

### Environment Variables

Copy the relevant `.env.example` to `.env` in each directory:

```bash
cp frontend/.env.example frontend/.env
```

Frontend env vars (`frontend/.env`):
- `VITE_SUPABASE_URL` — Supabase project URL
- `VITE_SUPABASE_ANON_KEY` — Supabase anonymous key

Backend env vars (root `.env` or shell):
- `GEMINI_API_KEY` — Google Gemini API key (required for AI analysis and chat — free at aistudio.google.com/apikey)
- `GEMINI_ANALYSIS_MODEL` — model for analysis (default: `gemini-2.0-flash`)
- `GEMINI_CHAT_MODEL` — model for chat (default: `gemini-2.0-flash`)
- `GITHUB_TOKEN` — GitHub personal access token (recommended to avoid rate limits)
- `MONGODB_URI` — MongoDB connection string (optional; falls back to in-memory storage)

### Backend

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend runs at `http://localhost:5173`
Backend API runs at `http://localhost:8000`

## API

### `POST /api/analyze`

Analyze a GitHub repository.

**Request body:**
```json
{ "repo_url": "https://github.com/owner/repo" }
```

**Response:** Structured `AnalysisResult` — see `backend/models/schemas.py`.

---

### `GET /api/analyze/{analysis_id}`

Retrieve a cached analysis by ID.

---

### `GET /api/explain/{analysis_id}`

Return full explainability report: pattern matches with files/keywords, AI inferences with reasoning, confidence breakdown.

---

### `POST /api/chat/`

Send a message to the repo-aware chat agent. Returns a streaming `text/event-stream` response.

**Request body:**
```json
{
  "analysis_id": "string",
  "session_id": "string (optional — omit to start a new session)",
  "message": "string"
}
```

**Streaming response:** Server-Sent Events. Each event is `data: {"chunk": "...", "session_id": "..."}`. Stream ends with `data: [DONE]`.

The agent is strictly grounded in the StackAnalysis for that repo. It will not invent features, generate documentation, or answer questions about things StackSniffer did not detect.

---

### `GET /api/chat/session/{session_id}`

Retrieve full conversation history for a session.

---

### `DELETE /api/chat/session/{session_id}`

Clear conversation history and reset the session.

---

### `GET /api/analyses/domain/{domain}`

Find recent analyses by domain (e.g. `web_api`, `ml_platform`, `fullstack`).

---

### `GET /api/analyses/pattern/{pattern}`

Find analyses matching a stack pattern string.

---

### `GET /api/health`

  Service status, AI availability, storage backend, and total analyses count.

## Chat Agent

The chat feature in the UI is powered by `gemini-2.0-flash` and grounded exclusively in the StackAnalysis for the analyzed repo. It knows:

- Every detected technology with confidence score and detection source
- AI inferences with the exact reasoning strings Gemini produced during analysis
- Pattern matches with matched files and keywords
- Domain, architecture style, stack pattern, notable combinations, and missing patterns
- Complexity score, files analyzed, patterns checked

It will not answer general coding questions or discuss things outside the detected stack. It is not a generic coding assistant.

The frontend chat panel supports:
- Real-time token streaming
- Per-session conversation history
- Text-to-speech (Web Speech API)
- Voice input via microphone (Web Speech API)
- Copy button on each AI response
- 6 repo-aware starter question pills auto-populated from the analysis

## Detection Categories

| Category   | Examples                                      |
|------------|-----------------------------------------------|
| Languages  | Python, TypeScript, Go, Rust, Java            |
| Frameworks | FastAPI, React, Next.js, Django, Spring Boot  |
| Databases  | PostgreSQL, MongoDB, Redis, Supabase          |
| Messaging  | Kafka, RabbitMQ, NATS, Celery                 |
| AI/ML      | PyTorch, TensorFlow, LangChain, Hugging Face  |
| Infra      | Docker, Kubernetes, Terraform, GitHub Actions |
| Testing    | pytest, Jest, Vitest, Cypress                 |
