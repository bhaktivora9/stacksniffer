# StackSniffer

A GitHub repository classification tool that analyzes a repo and labels it by software type, technology role, and architectural layer — while keeping deterministic, manifest-verified facts visually and structurally distinct from AI inferences at all times.

**Live demo:** __https://stacksniffer.vercel.app/__

**Version:** v0.1.0 (MVP)

---

## The thesis: provenance honesty

Every field StackSniffer shows is tagged by where it came from:

- **MAP** (manifest-verified) — read directly from a repo's manifests. Deterministic. Trustworthy.
- **AI** (model inference) — inferred by the classifier when the manifest doesn't say.

Verified facts outrank inference, and the UI never lets an AI guess masquerade as a verified fact. This one principle governs every design decision in the project: the two-tier detection pipeline, the color coding, the append-only correction ledger, and how novel categories are surfaced rather than silently coerced.

---

## What the MVP does (v0.1.0)

The core classification loop works end to end:

- **Two-tier detection** — manifest signals first (MAP), model inference second (AI), each tagged with its provenance.
- **13 canonical software types** — a single locked enum (`database`, `deployable_service`, `library`, `framework`, `build_tool`, and so on) is the one source of truth. The classifier prompt, the API, and the UI dropdown are all derived from it, so vocabulary can't drift between layers.
- **specific_identity** — when a repo fits a canonical type only loosely, the classifier records the narrower identity it perceived (e.g. a `deployable_service` whose purpose is model inference is tagged `model_serving`) without changing the shipped label. This captures near-miss signal for later review instead of discarding it.
- **Novelty routing** — a repo that fits no canonical type even loosely (a language runtime, an operating system) is flagged as novel and routed to a maintainer review queue, rather than being coerced into the nearest wrong bucket.
- **Correction ledger** — an append-only record of every correction and emergent-type proposal, with full provenance (who proposed, who approved, before/after values). Corrections apply as an overlay on top of the AI's original classification — the machine's judgment is never overwritten, only annotated.
- **Maintainer review queue** — a surface for adjudicating pending corrections and emergent-taxonomy proposals before they take effect. Known-but-inactive roles are distinguished from genuinely novel ones.
- **Advisory Layer 0 classifier (shadow mode)** — submitted software-type feedback trains a classifier that runs on every analysis alongside the primary path. It records what it would have predicted (`layer0_prediction`) and logs where it agrees or disagrees with the primary classification, but it does **not** yet override the final label. It is a shadow model being measured against production, not a decision-maker — see [Learned components](#learned-components-the-advisory-classifier) below.

## Stack

- **Backend:** Python, FastAPI, MongoDB, Gemini, a trained scikit-learn-style Layer 0 classifier (persisted to `software_type_classifier.pkl`)
- **Frontend:** React, Vite
- **Deployment:** Vercel (frontend), Render (backend), MongoDB Atlas (database)

---

## Learned components: the advisory classifier

StackSniffer trains a real software-type classifier from submitted feedback and runs it in the live analysis path — but deliberately in **advisory (shadow) mode**, not as the decision-maker. This is a safe-rollout pattern: the learned model earns the right to decide by first proving its agreement with the production path.

There are two separate feedback loops, and they do not connect — keeping them distinct is deliberate:

**Training loop.** A user's software-type correction is written to the `feedback` collection as a labeled example. The trainer reads these snapshots via `get_labeled_training_data()` and produces the classifier, persisted to `backend/models/software_type_classifier.pkl`. This loop feeds the model.

**Review / overlay loop.** A correction routed through the maintainer review queue, once approved, is applied as a provenance-bearing overlay on top of the AI's original output (recorded in the correction events / `software_type_corrections` ledger). This loop annotates the stored result — the machine's judgment is never overwritten — but it does **not** feed the trainer. Maintainer approval governs what the taxonomy and the displayed label become; it does not govern what the model learns.

How the advisory model runs:

1. On every analysis, the trained classifier produces a `layer0_prediction` alongside the primary (rule + Gemini) classification.
2. Where it disagrees with the primary path, the disagreement is recorded in `software_type_disagreements` (keyed to the analysis, so agreement rate is computable as a join, not a naive ratio).
3. The primary path still produces the final `software_type`. The learned model **observes and is measured**; it does not override.

**Promotion is gated on the disagreement analysis.** The learned model moves from advisory to primary only once its agreement with the production path — including on its own high-confidence predictions — justifies trusting it to decide. That gating step is future scope. Until then, the honest description is: a trained shadow model, running in production, tracked against the primary classifier, not yet promoted. The Analytics page surfaces the agreement/disagreement record so this readiness can be judged from evidence rather than asserted.

---

## Scope: what's deliberately deferred

StackSniffer was scoped around one thesis — provenance honesty — and everything that didn't serve that thesis was deferred on purpose. This is the MVP boundary, not an unfinished to-do list.

**Advanced (post-MVP) features:**

- **Dependency graph** — the one deferred item with a clean deterministic solution (`dependency-cruiser`, `pydeps`, `go mod graph`). Priced differently from the rest because it needs no model, just an off-the-shelf tool.
- **v3 relational model** — a technology × artifact × usage model producing multiple layer bindings per technology, replacing the current single-layer assignment. Documented as the target architecture, deferred until the taxonomy is stable.
- **Codebase-architecture platform features** — services, REST-endpoint mapping, database-entity extraction, service-interaction diagrams, and topology-dependent natural-language queries. These belong to a broader analyzer product and are explicitly out of MVP scope.

**Why this order:** the working sequence is prove the deterministic tier → stabilize the taxonomy → accumulate labeled feedback → train an advisory model and measure it in shadow → only promote it to deciding once its agreement rate justifies trust. The learned Layer 0 classifier already exists and runs in advisory mode (see above); what is deferred is *promoting* it from advisory to primary, which is gated on the disagreement analysis. Deferred items that depend on a stable taxonomy (the v3 model) wait for it; self-contained items (the dependency graph) can land independently.

---

## Known limitations

These are documented deliberately. Where the system can't determine something reliably, it declines to assert it rather than guessing — the same provenance-honest principle that governs the classification tiers. Each item below is a known boundary, not an undiscovered defect.

**Layer assignment is conservative (resolved by the v3 model).**
Architectural layer is contextual — it depends on which artifact a technology belongs to and how that artifact uses it. In multi-artifact repositories, a technology whose artifact ownership is ambiguous is marked *unassigned* rather than assigned to a guessed layer. This is intentional: an unassigned layer is the layer-axis equivalent of "don't let inference masquerade as a verified fact." The consequence is that large multi-artifact repos can show many unassigned technologies. The v3 relational model (technology × artifact × usage → layer bindings) is what resolves per-artifact ownership and will assign most of these correctly; until it lands, unassigned-over-wrong is the honest default.

**Dependency classification can time out on large manifests.**
The dependency-classification phase has a fixed time budget. Repositories with very large dependency manifests can exceed it, in which case the software-type verdict is made on partial dependency evidence and the result is flagged (`DEP_CLASSIFICATION_FAILED`, and confidence is flagged as overcalibrated when input coverage is low). The flag is surfaced rather than hidden, but affected runs should be treated as lower-confidence. Raising the budget or streaming the classification is future scope.

**AI-inferred detections require more scrutiny than manifest-verified ones.**
Technologies tagged **AI** (violet) are model inferences, not manifest-verified facts. Most detections are MAP (manifest-verified); a minority are AI-inferred, and inferred infrastructure technologies in particular (CI services, observability tools) are the category most prone to plausible-but-unverified guesses. The provenance tag exists precisely so these are visibly distinct and can be corrected — but a viewer should trust an AI-tagged detection less than an M-tagged one. Tightening the grounding for inferred detections is ongoing.

**Emergent-identity names can fragment before promotion.**
Emergent categories (specific_identity values and novel-type proposals) are free-form snake_case strings. The same underlying concept can arrive under slightly different names across repos (e.g. `git_bot` vs `code_review_bot`), which can fragment recurrence counts and delay promotion of a genuinely recurring category. Name-normalization/aliasing for emergent identities — the same discipline already applied to the canonical enum, one level down — is future scope. Until then, promotion decisions rely on a maintainer recognizing that differently-named proposals are the same concept.

**Free-tier deployment cold-starts.**
The hosted backend runs on a free tier that spins down after inactivity, so the first request after an idle period incurs a cold start of roughly a minute. This is a hosting-cost tradeoff, not a code limitation; a keep-warm ping or a paid always-on tier removes it.

---

## Versioning and release roadmap

StackSniffer follows [semantic versioning](https://semver.org). It is intentionally pre-1.0: the taxonomy is still evolving, so the API is not yet promised stable.

- **v0.1.0 — MVP** _(current)_. Core classification loop, provenance-honest UI, correction ledger, novelty routing, and a trained Layer 0 classifier running in advisory (shadow) mode.
- **v0.2.0+ — feature releases.** Each deferred item ships as a minor bump: promoting the advisory classifier once its agreement rate justifies it, the dependency graph, the v3 relational model, then the platform features.
- **v0.x.y — patches.** Bug fixes and taxonomy refinements.
- **v1.0.0 — stable.** Reached when the taxonomy is locked and the public API is committed to backward compatibility. Not before.

The pre-1.0 version is a deliberate, honest signal of maturity — consistent with the project's own provenance-honesty values. A `1.0` would claim a stability the taxonomy doesn't yet have.

---

## Configuration and secrets

Secrets are never committed to the repository. They are injected at runtime through the deployment platform's encrypted environment store (Render for the backend, Vercel for the frontend).

Required environment variables are documented in `.env.example` (placeholder values only — copy to `.env` for local development, which is gitignored):

**Backend (Render):**
- `GEMINI_API_KEY` — server-side only. Never exposed to the frontend or included in any build.
- `MONGODB_URI` — the Atlas connection string.
- `ALLOWED_ORIGINS` — comma-separated list of permitted frontend origins (CORS), such as the Vercel app URL. Do not use `*`; credentialed CORS is restricted to explicit origins.
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — server-side maintainer credentials for admin mutations. The backend verifies them at `/api/review/login`, then issues a role-bearing bearer token for guarded approve/reject/promote/submit routes. Set these only on Render or in the root local `.env`; never add them to `frontend/.env`, never prefix them with `VITE_`, and never expose them to browser code.

**Frontend (Vercel):**
- `VITE_API_BASE_URL` — the public backend URL. Safe to expose (it's public anyway); this is why the Gemini key must never be a `VITE_`-prefixed variable — anything prefixed `VITE_` ships to the browser.

If a key is ever committed or leaked, rotate it in the provider's console and update the platform env store — no code change required.

**Future scope (enterprise secret management):** for a production deployment beyond a portfolio demo, the platform env store would be replaced by a dedicated secrets manager (GCP Secret Manager, AWS Secrets Manager, or Vault), with the backend retrieving credentials via a platform identity rather than a stored variable, enabling central rotation without redeploys.

---

## Local development

```
# Backend
cp .env.example .env    # from repo root; fill in server-side secrets, including ADMIN_USERNAME and ADMIN_PASSWORD
cd backend
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000

# Frontend
cd frontend
npm install
cp .env.example .env    # set VITE_API_BASE_URL=http://localhost:8000
npm run dev
```

Backend runs at `localhost:8000`, frontend at `localhost:5173`.

Admin mutation check:

```
# No session: should return 403
curl -i -X POST http://localhost:8000/api/review/<review_item_id>/approve \
  -H "Content-Type: application/json" \
  -d "{\"target_value\":\"library\"}"

# Valid admin session: login returns a bearer token.
# POST /api/review/session is not a login route; use GET /api/review/session to verify a token.
TOKEN=$(curl -s -X POST http://localhost:8000/api/review/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$ADMIN_USERNAME\",\"password\":\"$ADMIN_PASSWORD\"}" | jq -r .access_token)

curl -i http://localhost:8000/api/review/session \
  -H "Authorization: Bearer $TOKEN"

curl -i -X POST http://localhost:8000/api/review/<review_item_id>/approve \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"target_value\":\"library\"}"
```
