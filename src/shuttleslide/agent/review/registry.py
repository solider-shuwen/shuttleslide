"""StageRegistry — discoverable, anchor-ordered stage pipeline.

The registry is the source of truth for which stages run and in what
order. Core stages (theme/outline/images/slides/rendered) are
registered by default; external packages add more via the
``shuttleslide.review.stages`` entry-point group:

.. code-block:: toml

    # pyproject.toml of the external package
    [project.entry-points."shuttleslide.review.stages"]
    script = "my_package.stages:ScriptStage"

Resolution model
----------------
Each Stage may declare ``after`` and / or ``before`` — names of other
stages it must run after / before. The registry compiles these into a
DAG and produces a linear order via topological sort.

Tie-breaking rules (deterministic, mirror ``cli_registry`` semantics):

1. Stages with no relative constraint between them are ordered by
   **registration order**. Core stages register first; entry-point
   stages register afterwards in entry_points() iteration order.
2. ``after``/``before`` referencing an unknown name is a **warning**,
   not an error — the constraint is silently dropped. This keeps a pro
   stage robust to a missing core stage (e.g. user disabled ``images``).
3. A cycle in the DAG is a ``RegistryError``. The orchestrator catches
   it at construction time and falls back to a core-only registry.

Failure isolation
-----------------
``load_extensions`` mirrors ``extensions/cli_registry.register_extensions``:
each entry point load is wrapped in try/except; broken extensions log
to stderr and are skipped, the rest still register.
"""

from __future__ import annotations

import sys
from importlib.metadata import EntryPoint, entry_points
from typing import Dict, Iterable, List, Optional, Tuple

from shuttleslide.agent.review.stage import Stage


ENTRY_POINT_GROUP = "shuttleslide.review.stages"


class RegistryError(Exception):
    """Raised when the registry cannot resolve a valid stage order.

    Cycle / multiple terminals / no terminal all surface here. The
    orchestrator catches this and falls back to a core-only registry.
    """


class StageRegistry:
    """Ordered collection of ``Stage`` instances.

    Instances are owned by the registry — callers should not mutate a
    stage after registering it. The registry is intentionally not
    thread-safe; orchestrators build one per pipeline run.
    """

    def __init__(self) -> None:
        # Insertion-ordered. Stable iteration matters for tie-breaking.
        self._stages: List[Stage] = []
        self._by_name: Dict[str, Stage] = {}
        # Cached resolution; invalidated on every register() call.
        self._resolved_cache: Optional[List[Stage]] = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, stage: Stage) -> None:
        """Add a stage. Raises ``ValueError`` on duplicate name.

        Core stages register first (in canonical order); pro stages
        come afterwards via ``load_extensions``. The orchestrator's
        fallback path constructs a fresh registry and re-registers
        only the core stages, so a broken pro stage cannot wedge the
        pipeline.
        """
        name = stage.name
        if not name:
            raise ValueError(
                f"stage {stage!r} has empty name; refusing to register"
            )
        if name in self._by_name:
            # First-registered wins, mirroring cli_registry's
            # "core wins on name conflict" rule. Callers wishing to
            # override should construct a fresh registry rather than
            # mutate this one.
            raise ValueError(
                f"stage {name!r} already registered "
                f"(existing={type(self._by_name[name]).__name__}, "
                f"new={type(stage).__name__})"
            )
        # Validate anchor names shape early — actual reference checks
        # happen at resolve_order time so unknown anchors can be
        # downgraded to warnings after all stages are registered.
        for attr in ("after", "before"):
            v = getattr(stage, attr, None)
            if v is not None and not isinstance(v, str):
                raise TypeError(
                    f"stage {name!r}.{attr} must be str or None, "
                    f"got {type(v).__name__}"
                )
        self._stages.append(stage)
        self._by_name[name] = stage
        self._resolved_cache = None

    def get(self, name: str) -> Stage:
        """Look up a stage by name. Raises ``KeyError`` if unknown."""
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(
                f"stage {name!r} not registered; known: {self.all_names()}"
            )

    def all_names(self) -> Tuple[str, ...]:
        """All registered stage names in registration order."""
        return tuple(s.name for s in self._stages)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve_order(self) -> List[Stage]:
        """Return stages in execution order.

        Computes the order once and caches; subsequent calls return the
        cache until ``register`` invalidates it.
        """
        if self._resolved_cache is not None:
            return list(self._resolved_cache)

        if not self._stages:
            self._resolved_cache = []
            return []

        # Validate exactly one terminal stage. Multiple terminals make
        # "the result" ambiguous; zero terminals means the pipeline
        # has no output. Both surface as RegistryError so the
        # orchestrator can fall back to core-only.
        terminals = [s for s in self._stages if getattr(s, "terminal", False)]
        if len(terminals) > 1:
            raise RegistryError(
                f"multiple terminal stages: {[s.name for s in terminals]}"
            )
        if not terminals:
            raise RegistryError(
                "no terminal stage registered; pipeline needs exactly one"
            )

        # Build adjacency: edges go "from prerequisite -> dependent".
        # after=X means X must run before this stage → edge X -> name.
        # before=X means this stage must run before X → edge name -> X.
        # Unknown anchors are dropped with a warning (per docstring rule 2).
        names = {s.name for s in self._stages}
        edges: Dict[str, List[str]] = {n: [] for n in names}
        indegree: Dict[str, int] = {n: 0 for n in names}

        for s in self._stages:
            after = getattr(s, "after", None)
            if after is not None:
                if after not in names:
                    print(
                        f"[shuttleslide] warning: stage {s.name!r} "
                        f"declares after={after!r} which is not registered; "
                        f"constraint ignored",
                        file=sys.stderr,
                    )
                else:
                    edges[after].append(s.name)
                    indegree[s.name] += 1
            before = getattr(s, "before", None)
            if before is not None:
                if before not in names:
                    print(
                        f"[shuttleslide] warning: stage {s.name!r} "
                        f"declares before={before!r} which is not registered; "
                        f"constraint ignored",
                        file=sys.stderr,
                    )
                else:
                    edges[s.name].append(before)
                    indegree[before] += 1

        # Kahn's algorithm. To preserve registration order on ties,
        # process zero-indegree nodes in the order they were registered
        # rather than using a set / heap.
        ordered: List[str] = []
        # Work on a copy so repeated resolve_order calls are stable.
        pending = list(self._stages)
        indeg = dict(indegree)

        while pending:
            # Find the next zero-indegree stage in registration order.
            progressed = False
            for i, s in enumerate(pending):
                if indeg[s.name] == 0:
                    ordered.append(s.name)
                    for dep in edges[s.name]:
                        indeg[dep] -= 1
                    del pending[i]
                    progressed = True
                    break
            if not progressed:
                # Cycle. Report the offending stages.
                cycle_names = [s.name for s in pending]
                raise RegistryError(
                    f"cycle in stage anchors among: {cycle_names}"
                )

        by_name = self._by_name
        self._resolved_cache = [by_name[n] for n in ordered]
        return list(self._resolved_cache)


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


def iter_stage_entry_points() -> Iterable[EntryPoint]:
    """Yield all registered ``shuttleslide.review.stages`` entry points.

    Wrapper so tests can monkeypatch one place.
    """
    try:
        return entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — very old Python shape
        return entry_points().get(ENTRY_POINT_GROUP, [])


def load_extensions(registry: StageRegistry) -> StageRegistry:
    """Load every entry-point stage into ``registry``.

    Mirrors ``extensions/cli_registry.register_extensions``:
    - Each load + instantiate is wrapped in try/except; failures log
      to stderr and the offending entry is skipped.
    - Name conflicts with an already-registered stage log a warning
      and skip — first-registered wins (core stages register first,
      so pro cannot shadow a builtin).
    """
    for ep in iter_stage_entry_points():
        try:
            stage_cls = ep.load()
        except Exception as exc:  # noqa: BLE001 — extension isolation
            print(
                f"[shuttleslide] failed to load review stage '{ep.name}' "
                f"from {ep.value}: {exc}",
                file=sys.stderr,
            )
            continue
        try:
            stage = stage_cls()  # type: ignore[call-arg]
        except Exception as exc:  # noqa: BLE001 — extension isolation
            print(
                f"[shuttleslide] failed to instantiate review stage "
                f"'{ep.name}' ({stage_cls!r}): {exc}",
                file=sys.stderr,
            )
            continue
        # Validate it satisfies the Stage contract (duck-typed). We
        # check name + run; other attributes default on StageBase and
        # may be absent on bare Protocol implementations.
        if not hasattr(stage, "name") or not callable(getattr(stage, "run", None)):
            print(
                f"[shuttleslide] review stage '{ep.name}' loaded "
                f"{stage!r} which does not satisfy the Stage protocol; "
                f"skipping",
                file=sys.stderr,
            )
            continue
        try:
            registry.register(stage)
        except ValueError as exc:
            # Most likely: name collision with a core stage.
            print(
                f"[shuttleslide] extension stage '{ep.name}' "
                f"skipped: {exc}",
                file=sys.stderr,
            )
            continue
    return registry


# ---------------------------------------------------------------------------
# Default registry (core stages only) + full registry (with extensions)
# ---------------------------------------------------------------------------


_default_singleton: Optional[StageRegistry] = None


def default_registry() -> StageRegistry:
    """Core-only registry: the 5 built-in stages in canonical order.

    Returns a module-level singleton so callers can share the same
    Stage instances (they're stateless across runs). To add or remove
    stages, build a fresh ``StageRegistry`` and pass it to the
    orchestrator explicitly.
    """
    global _default_singleton
    if _default_singleton is None:
        # Local import to avoid import cycle: core_stages imports
        # state.py + schema.py + nodes/*, none of which import registry.
        # But registry is imported by orchestrator which is imported
        # by review.__init__ — keeping the core stage imports inside
        # the function lets ``default_registry()`` callers avoid
        # pulling them transitively until they actually need them.
        from shuttleslide.agent.review.core_stages import (
            ImagesStage,
            OutlineStage,
            RenderedStage,
            SlidesStage,
            ThemeStage,
        )

        reg = StageRegistry()
        reg.register(ThemeStage())
        reg.register(OutlineStage())
        reg.register(ImagesStage())
        reg.register(SlidesStage())
        reg.register(RenderedStage())
        _default_singleton = reg
    return _default_singleton


def full_registry() -> StageRegistry:
    """Fresh registry with core + entry-point extensions loaded.

    Always builds new — unlike ``default_registry``, this is not a
    singleton, because entry-point loading may surface different
    stages depending on which packages are installed at call time
    (think: tests that monkeypatch the entry-point iterator).

    Used by the orchestrator at construction time. The orchestrator
    catches ``RegistryError`` and falls back to ``default_registry``
    so a broken pro stage cannot wedge the pipeline.
    """
    reg = StageRegistry()
    # Register core stages first so name conflicts with pro extensions
    # resolve in favour of the builtins.
    from shuttleslide.agent.review.core_stages import (
        ImagesStage,
        OutlineStage,
        RenderedStage,
        SlidesStage,
        ThemeStage,
    )
    reg.register(ThemeStage())
    reg.register(OutlineStage())
    reg.register(ImagesStage())
    reg.register(SlidesStage())
    reg.register(RenderedStage())
    load_extensions(reg)
    return reg
