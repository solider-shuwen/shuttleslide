"""Skill asset discovery via entry points (design — not yet wired).

Reserved for a follow-up PR. When ``shuttleslide-pro`` (or any external
package) wants to ship its own Claude Code skill files alongside the
built-in ones in :mod:`shuttleslide.skills`, it registers a callable under
the ``shuttleslide.skill_assets`` entry-point group:

.. code-block:: toml

    # pyproject.toml of the external package
    [project.entry-points."shuttleslide.skill_assets"]
    pro-skills = "shuttleslide_pro.skills:iter_skill_dirs"

The callable takes no arguments and returns an iterable of
``pathlib.Path``, each pointing to a directory that contains a
``SKILL.md`` file (i.e. structured the same way as the built-in skill
dirs under ``src/shuttleslide/skills/``).

``slidecraft install-skill`` will iterate these directories and deploy
them alongside the built-in skills, using the same idempotency /
``--force`` / ``--path`` semantics.

Design rationale (why this isn't implemented yet):
- The public ``shuttleslide`` repo ships three skills today
  (``slidecraft``, ``slidecraft-to-html``, ``slidecraft-review``) which
  cover the public CLI surface. There is no public external consumer.
- ``shuttleslide-pro`` is the only near-term consumer and can ship its
  own ``install-skill`` step until the entry-point contract stabilizes.
- Adding the group now without a consumer means the contract is set in
  stone before any second consumer validates it.

This module therefore exposes the group name and a stub iterator so the
``install-skill`` command has a single integration point to call once
the contract is validated.

Contract notes for the future implementer:
- Follow the failure-isolation pattern in
  :mod:`shuttleslide.extensions.cli_registry` — a provider that raises
  logs to stderr and is skipped; other providers still load.
- Skill directory names that collide with built-in skills (``slidecraft``,
  ``slidecraft-to-html``, ``slidecraft-review``) are skipped; built-ins
  always win. Same rule as ``cli_registry``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

# Public entry-point group name. Stable across versions once the first
# external consumer ships against it. Do NOT rename without coordinating
# a deprecation cycle.
ENTRY_POINT_GROUP = "shuttleslide.skill_assets"


def iter_extension_skill_dirs() -> Iterable[Path]:
    """Yield skill directories registered via ``shuttleslide.skill_assets``.

    Not yet implemented — returns an empty iterable. Listed as a stub so
    ``install-skill`` already calls into this function; flipping the
    implementation on is a one-liner.
    """
    return ()
