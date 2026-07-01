"""Per-target LLM chat history with LRU eviction + message cap.

Each ``EditTarget.path`` (e.g. ``("slide", 3, "slot", "hero")``) gets
its own conversation thread. When the user says "make it bluer" then
"actually use the brand blue instead", the second edit needs the first
to make sense — without per-target history the LLM would treat each
edit as independent.

Memory bounds
-------------
Two limits keep memory predictable:

  * ``MAX_TARGETS = 50`` — LRU across targets. A user rarely touches
    more than a handful of elements in a session, but a long-running
    studio session editing all slides could exceed that. LRU eviction
    discards the oldest unused target's history entirely.
  * ``MAX_MESSAGES_PER_TARGET = 6`` — keep only the last 6 messages
    (3 user + 3 assistant, roughly). Older turns get popped from the
    head. Combined with the system prompt that re-embeds the current
    value each call, this is enough context for iterative refinement
    without token explosion.

Persistence
-----------
In-memory only. PR3 doesn't survive process restarts — on resume the
history is empty, but the ``AgentState`` loaded from disk has the
cumulative effect of prior edits, so the LLM still sees the right
current value. Only the chat dialog itself is lost.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable, List, Tuple

# path is hashed for dict key because tuples of mixed-type values
# (int / str) aren't natively hashable in a stable way... actually they
# are. Keep it as the raw tuple — that way _resolve_target on the server
# can pass the same tuple it pulled from the snapshot without conversion.
Path = Tuple

MAX_TARGETS = 50
MAX_MESSAGES_PER_TARGET = 6


class SessionStore:
    """LRU of per-target chat histories.

    Lookup, append, and LRU-eviction are all O(1). No locking — this
    store is only mutated from inside ``apply_edit`` / ``apply_llm_edit``
    which run single-threaded on the orchestrator loop (the server
    dispatches edits there).
    """

    def __init__(
        self,
        max_targets: int = MAX_TARGETS,
        max_messages_per_target: int = MAX_MESSAGES_PER_TARGET,
    ) -> None:
        self._store: "OrderedDict[Path, List[dict]]" = OrderedDict()
        self._max_targets = max_targets
        self._max_messages = max_messages_per_target

    def get(self, path: Path) -> List[dict]:
        """Return the chat history for ``path`` (may be empty).

        Marks ``path`` as most-recently-used (LRU bookkeeping). Returns
        a COPY so callers can mutate without affecting the stored list
        until they explicitly call ``append``.
        """
        history = self._store.get(path)
        if history is None:
            return []
        self._store.move_to_end(path)
        return list(history)

    def append(self, path: Path, role: str, content: str) -> None:
        """Append ``{role, content}`` to ``path``'s history.

        Trims to the most recent ``max_messages_per_target`` messages
        after append. If ``path`` is new and the store is full, the
        least-recently-used target is evicted.
        """
        existing = self._store.get(path)
        if existing is None:
            existing = []
            self._store[path] = existing
            if len(self._store) > self._max_targets:
                # popitem(last=False) evicts the LRU entry
                self._store.popitem(last=False)
        existing.append({"role": role, "content": content})
        # Trim head to keep only the last N messages.
        if len(existing) > self._max_messages:
            del existing[: len(existing) - self._max_messages]
        self._store.move_to_end(path)

    def clear(self, path: Path) -> None:
        """Drop ``path``'s history entirely.

        Called by tests between cases; in production it's a no-op since
        sessions die with the process.
        """
        self._store.pop(path, None)

    def __len__(self) -> int:
        """Number of tracked targets."""
        return len(self._store)

    def messages_for(self, path: Path) -> Iterable[dict]:
        """Read-only view of ``path``'s messages (no LRU update).

        Used by the server when serialising chat history to the WS client.
        """
        history = self._store.get(path)
        return list(history) if history else []
