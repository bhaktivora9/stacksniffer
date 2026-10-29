# R1 retrieval benchmark: labelling rubric (rubric/1)

This rubric governs the questions and relevance judgments of the R1 retrieval benchmark (SS-ML-303).
Every judgment records `annotator_version: rubric/1`. A change to this document after the dataset is
frozen means a new rubric version and a new dataset version; frozen judgments are never edited.

## Corpus and roles

Repositories come from `repositories.yaml`:

| Role | Repositories | Used for |
|---|---|---|
| TEST | spring-petclinic (Java), full-stack-fastapi-template (Python, `backend/` only) | Reported metrics only. Never inspected while tuning retrieval, graph expansion or GraphRAG. |
| VALIDATION | spring-boot-realworld (Java), flask (Python) | Tuning and miss analysis. |

Test questions are written and frozen before any graph-aware retrieval is tuned. If a test question
turns out to be broken after freezing, it is excluded in the next dataset version, with the reason
recorded. It is never silently corrected to improve a metric.

## Question classes

Each question records `metadata.question_class`:

- **local**: answered by one or a few specific symbols or one flow, usually two to four entities that
  call each other. Scope `SYMBOL` or `FILE`. Example: "How does the API verify a bearer token and
  load the calling user?"
- **global**: about the repository as a whole: architecture, configuration, deployment, data model,
  cross-cutting concerns. Scope `MODULE` or `REPOSITORY`. The answer spans several files, often
  including configuration or SQL.

Aim for roughly two local questions for every global one per repository.

`task_category` is one of `code_search`, `flow`, `configuration`, `architecture`, `data_model`.

## Writing questions

- Write the question a new engineer on the project would ask, in plain language.
- **Avoid identifier giveaways.** Do not put class, method or file names in the question unless the
  question is about that identifier. Questions that do share words with the code are tagged
  `metadata.lexical_overlap: high`, so lexical matches can be analysed separately in the miss
  categories.
- Every question must have at least one grade-3 target that exists at the frozen commit.
- No two questions may share all their grade-3 targets.

## Grades

Judgments target canonical facts, never chunks: an `entity` stable key, or a `file` with a
`lines` range (`[start, end]`, inclusive) for code outside any declaration (configuration, SQL,
module-level code). A retrieved chunk satisfies a judgment if it belongs to the judged entity or
overlaps the judged range.

| Grade | Meaning | Test |
|---|---|---|
| 3 | **Answers it.** Reading this target alone answers the question or its central part. | Would you quote it as the answer? |
| 2 | **Necessary context.** Part of the answer, or required to understand the grade-3 target (the callee that does the check, the config the code reads). | Would the answer be incomplete without it? |
| 1 | **Related.** On topic and useful background, but the question is answerable without it. | Would you mention it in a "see also"? |
| 0 | Not relevant. Recorded only for near misses worth tracking (for example, a same-named method elsewhere). | |

Rules:

- Judge the smallest canonical target that carries the meaning: the method rather than its class,
  or the property lines rather than the whole file.
- A class gets grade 3 only when the question is about the class as a whole (its fields, its role
  or its hierarchy).
- Tests are not targets unless the question asks about tests.
- Generated and vendored code is never a target. Default retrieval excludes it.

## Review

1. **First pass.** Questions and judgments are drafted with `judgment_source: MODEL_ASSISTED`.
2. **Second pass (human).** For every question, the reviewer reads each target at the frozen commit
   and confirms or changes its grade. They also look for missing grade-2 or grade-3 targets by
   searching the code, not only the drafted list. Reviewed judgments become `judgment_source: HUMAN`
   with the reviewer as `annotator`. Disagreements are noted in `notes`.
3. **Freeze.** Only a dataset whose judgments are all reviewed is loaded and frozen. The frozen
   fingerprint is recorded with the vector-only baseline run.

## Miss categories (analysis after the baseline run)

Each relevant target missing from the top 10 is assigned one category: `chunking` (target split or
merged badly), `embedding` (right chunk, poor similarity), `missing_edge` (reachable only through a
relation the index cannot see), `graph_distance` (two or more hops from anything retrieved),
`lexical` (the question's wording differs from the code's), or `annotation` (the judgment itself is
wrong; fixed only in a new dataset version).
