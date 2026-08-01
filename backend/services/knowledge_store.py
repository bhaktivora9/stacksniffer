"""
backend/services/knowledge_store.py

Single owner of StackSniffer's deterministic dependency knowledge.

SCOPE — read-only access to the five live ecosystem dependency tables loaded
from config/dependency_knowledge.json. Classification mechanism remains in
dep_fallback.py.

DELIBERATELY OUT OF SCOPE (see conversation / build plan):
  - patterns.json: taxonomy administration with live writers (discovery.py,
    stack_feedback_service.py, learning_service.py). Different knowledge software_type,
    different write profile, NOT on the layer-map critical path. Enters the store
    only if/when we centralize taxonomy admin.
  - stack_detector.py's _PATTERNS: confirmed dead path and removed. Excluded.

A later storage-backed step can swap the seed-file source for Mongo and add the
gated write-back path for approved learning. The interface remains stable when
the source changes, so classification code stays independent of persistence.
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
