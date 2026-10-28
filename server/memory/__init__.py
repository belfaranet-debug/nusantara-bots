"""Memory package for the Grok Bot own build.

Headline differentiator vs the real Grok Bot app: this memory works
with ZERO providers. No model API, no network, no vector service --
keyword + recency recall over a local SQLite file, so a character
with no brain connection still remembers yesterday. That floor
guarantee is the feature; embeddings, if ever added, are only an
optional accelerator, never a dependency.

Import (from ``~/workspace/grokbot-own/``)::

    from memory.store import MemoryStore
    from memory import store  # module-level remember/recall/list/forget

See ``store.py`` for the full API and scope/supersede rules.
"""

from memory.store import (  # noqa: F401
    DEFAULT_DB_PATH,
    TEAM_SCOPE,
    MemoryStore,
    forget,
    list_memories,
    private_scope,
    recall,
    remember,
    validate_scope,
)

__all__ = [
    "MemoryStore",
    "DEFAULT_DB_PATH",
    "TEAM_SCOPE",
    "private_scope",
    "validate_scope",
    "remember",
    "recall",
    "list_memories",
    "forget",
]
