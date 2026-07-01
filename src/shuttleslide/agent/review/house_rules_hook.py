"""House-rules override hook — extension point for prompt customization.

External packages register a provider that returns a canvas-specific
(or any-mode-specific) ``HOUSE_RULES`` string, swapped into
``shuttleslide.agent.prompts.HOUSE_RULES`` for the duration of one
review-pipeline run. The first consumer is the private ``shuttleslide-pro``
package, whose ``canvas_house_rules_provider`` reads
``AgentConfig.canvas_aspect_ratio`` and returns a canvas-aware house_rules
template; other providers can layer in on top.

Registration (in the external package's ``pyproject.toml``):

.. code-block:: toml

    [project.entry-points."shuttleslide.review.house_rules"]
    canvas = "my_package.hooks:my_house_rules_provider"

Provider protocol: a callable taking an :class:`AgentConfig` and returning
either ``None`` (no override; defer to other providers) or a ``str``
(override ``HOUSE_RULES`` for this run). When multiple providers return
non-``None``, the last one in entry-point-iteration order wins — this
matches how a single ``HOUSE_RULES`` module constant can only hold one
value at a time. Iteration order is the order ``importlib.metadata``
returns, which is stable per process.

Failure isolation: a provider raising → logs to stderr, that provider is
skipped, others still run, ``HOUSE_RULES`` keeps whatever value prior
providers set (or the original if none did). This mirrors
:func:`shuttleslide.extensions.cli_registry.register_extensions` — a
broken extension must never crash the host process.

This is the second officially-supported extension point after
``shuttleslide.cli_commands``. Adding new entry-point groups requires a
deliberate API-design decision and a CLAUDE.md update; this one is stable.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Callable, Iterator, Optional

if TYPE_CHECKING:
    from shuttleslide.agent.config import AgentConfig


# Public alias documenting the expected provider signature. Imported by
# external packages via ``from shuttleslide.agent.review.house_rules_hook
# import HouseRulesProvider`` — kept as a ``Callable`` alias rather than a
# ``Protocol`` class so external packages don't need ``typing.Protocol``
# at module-load time (some still target 3.9 syntax).
HouseRulesProvider = Callable[["AgentConfig"], Optional[str]]


ENTRY_POINT_GROUP = "shuttleslide.review.house_rules"


def _iter_entry_points():
    """Yield registered entry points in the house_rules group.

    Wrapper so tests can monkeypatch one place. Same try/except shape as
    :func:`shuttleslide.extensions.cli_registry.iter_extension_entry_points`.
    """
    try:
        return entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — very old Python shape
        return entry_points().get(ENTRY_POINT_GROUP, [])


def resolve_house_rules(config: "AgentConfig") -> Optional[str]:
    """Ask every registered provider for a house_rules override.

    Returns the last non-``None`` value (entry-point iteration order), or
    ``None`` if no provider returned a value (caller leaves the module
    constant alone). Failures in individual providers are logged to stderr
    and skipped — same isolation contract as the CLI extension registry.
    """
    result: Optional[str] = None
    for ep in _iter_entry_points():
        try:
            provider = ep.load()
        except Exception as exc:  # noqa: BLE001 — provider isolation
            print(
                f"[shuttleslide] failed to load house_rules provider "
                f"'{ep.name}' from {ep.value}: {exc}",
                file=sys.stderr,
            )
            continue
        try:
            value = provider(config)
        except Exception as exc:  # noqa: BLE001 — provider isolation
            print(
                f"[shuttleslide] house_rules provider '{ep.name}' raised: {exc}",
                file=sys.stderr,
            )
            continue
        if value is not None:
            result = value
    return result


@contextmanager
def override_house_rules_for_config(
    config: "AgentConfig",
) -> Iterator[Optional[str]]:
    """Context manager: swap ``HOUSE_RULES`` for the duration of a run.

    Resolves the override via :func:`resolve_house_rules` (querying all
    registered providers). When the resolved value is ``None`` the module
    constant is left untouched. Otherwise the new value replaces
    ``shuttleslide.agent.prompts.HOUSE_RULES`` until the context exits,
    when the original is restored — even if the body raises.

    The resolved value (or ``None``) is yielded so callers can log it or
    inspect what was applied.

    Not thread-safe: the swap is process-wide. The review server runs
    one pipeline at a time (enforced by ``POST /api/start`` returning 409
    when a pipeline is already running), so this constraint is satisfied
    by the existing API contract.
    """
    import shuttleslide.agent.prompts as _prompts

    original = _prompts.HOUSE_RULES
    override = resolve_house_rules(config)
    if override is not None:
        _prompts.HOUSE_RULES = override
    try:
        yield override
    finally:
        _prompts.HOUSE_RULES = original
