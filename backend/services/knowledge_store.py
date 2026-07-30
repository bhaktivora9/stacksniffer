"""
backend/services/knowledge_store.py

Single owner of StackSniffer's deterministic dependency knowledge.

STEP (a) SCOPE — read-only indirection over the five live ecosystem dep tables
currently defined in dep_fallback.py. This step changes WHERE the tables come
from, not WHAT they contain: same entries, same post-_seed_table shape, same
classifications. 213 tests must stay green — any behavior change is a bug.

DELIBERATELY OUT OF SCOPE (see conversation / build plan):
  - patterns.json: taxonomy administration with live writers (discovery.py,
    stack_feedback_service.py, learning_service.py). Different knowledge domain,
    different write profile, NOT on the layer-map critical path. Enters the store
    only if/when we centralize taxonomy admin.
  - stack_detector.py's _PATTERNS: confirmed dead path, never imported. Excluded.

STEP (b) — later — swaps load() to read a seed file (seeds/knowledge.yaml)
instead of the in-code dicts, satisfying "no knowledge hardcoded in logic".
STEP (c) — later — swaps the seed-file source for Mongo, adding the gated
write-back path that lets the learning loop persist approvals.

The interface below does not change across (a)->(b)->(c). Only load()'s source
changes. That is the point of introducing the store now: it fixes the seam so
the later data-source swaps touch one method, not every call site.
"""

from __future__ import annotations

import json
from pathlib import Path

_SEED_PATH = Path(__file__).parent.parent / "config" / "dependency_knowledge.json"


class KnowledgeStore:
    """Serves deterministic dependency knowledge from an in-memory projection.

    Read-mostly: loaded once at import, read on the hot path (per dependency,
    per analysis). The hot path never does I/O — it reads the dict held here.
    """

    def __init__(self, dep_tables: dict[str, dict[str, dict]]):
        # dep_tables: ecosystem_name -> { tech_key -> {layer, multi_role, secondary} }
        # i.e. the POST-_seed_table shape, keyed per raw ecosystem table.
        self._dep_tables = dep_tables

    @classmethod
    def load(cls) -> "KnowledgeStore":
        """Load the versioned, read-only dependency seed."""
        with _SEED_PATH.open(encoding="utf-8") as seed_file:
            payload = json.load(seed_file)
        tables = payload.get("ecosystems")
        if not isinstance(tables, dict) or not tables:
            raise ValueError("dependency knowledge seed has no ecosystems")
        return cls(dep_tables=tables)

    def dep_tables(self) -> dict[str, dict[str, dict]]:
        """The five seeded ecosystem tables, keyed by ecosystem name.

        Returned by reference intentionally: these tables are WRITE-FREE at
        runtime (nothing mutates them after load), so no defensive copy is
        needed for step (a). If a future writer appears (it will not for dep
        tables — writes go through the taxonomy path), this contract changes.
        """
        return self._dep_tables


# Module singleton. Cheap, read-mostly, available at import time for the free
# functions in dep_fallback.py and for tests that import those functions
# directly with no application lifecycle. Not FastAPI app.state — that would be
# dependency injection for static knowledge threaded through synchronous free
# functions, which is ceremony with no benefit here.
store = KnowledgeStore.load()
