"""Dynamic CLI command discovery via entry points.

External packages register Click commands that augment ``slidecraft``:

.. code-block:: toml

    # pyproject.toml of the external package
    [project.entry-points."shuttleslide.cli_commands"]
    narrate = "my_package.cli:narrate"

At process start, :func:`register_extensions` scans that group and attaches
each command to the main Click group. Failures are logged to stderr but do
not abort the CLI — a broken extension should not make the whole tool
unusable.

This module has zero knowledge of what extensions exist; it only provides
the discovery machinery. The first real consumer is the private
``shuttleslide-pro`` package.
"""

from __future__ import annotations

import sys
from importlib.metadata import EntryPoint, entry_points
from typing import Iterable

import click


ENTRY_POINT_GROUP = "shuttleslide.cli_commands"


def iter_extension_entry_points() -> Iterable[EntryPoint]:
    """Yield all registered entry points in the ``shuttleslide.cli_commands`` group.

    Wrapper around :func:`importlib.metadata.entry_points` so tests can
    monkeypatch one place.
    """
    try:
        # Python 3.10+ returns a SelectableGroups; entry_points(group=...) is
        # the stable selector across 3.9–3.12.
        return entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — very old Python shape
        return entry_points().get(ENTRY_POINT_GROUP, [])


def register_extensions(group: click.Group) -> click.Group:
    """Attach every registered extension command to ``group``.

    Called once at process start from :mod:`shuttleslide.cli`. Safe to call
    when no extensions are registered (no-op). Safe to call when an
    extension's entry point fails to load (logs to stderr, continues).

    Args:
        group: The Click ``Group`` to attach commands to (typically ``main``).

    Returns:
        The same ``group`` (for chaining / decorator-style use).
    """
    for ep in iter_extension_entry_points():
        try:
            cmd = ep.load()
        except Exception as exc:  # noqa: BLE001 — extension isolation
            click.echo(
                f"[shuttleslide] failed to load extension '{ep.name}' "
                f"from {ep.value}: {exc}",
                err=True,
            )
            continue

        # Accept either a Command or a Group. Both inherit from the same base
        # in Click 8 (BaseCommand) and Click 9 (Command); checking the two
        # concrete types we actually support keeps us forward-compatible
        # without touching the deprecated BaseCommand alias.
        if not isinstance(cmd, (click.Command, click.Group)):
            click.echo(
                f"[shuttleslide] extension '{ep.name}' "
                f"loaded {cmd!r} which is not a click.Command; skipping.",
                err=True,
            )
            continue

        if ep.name in group.commands:
            click.echo(
                f"[shuttleslide] extension '{ep.name}' conflicts with an "
                f"existing command; skipping.",
                err=True,
            )
            continue

        group.add_command(cmd, name=ep.name)

    return group


def _entry_points_for_test() -> tuple[EntryPoint, ...]:
    """Test helper: snapshot current registrations as a tuple."""
    return tuple(iter_extension_entry_points())


def _is_interactive() -> bool:
    """Whether stderr is a TTY — affects how loud we are about failures."""
    return sys.stderr.isatty()
