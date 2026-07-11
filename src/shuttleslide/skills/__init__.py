"""Built-in Claude Code skill assets shipped with shuttleslide.

Each subdirectory contains a ``SKILL.md`` file that ``slidecraft install-skill``
deploys to ``~/.claude/skills/`` so Claude Code (and other agent harnesses
that consume the same format) can discover and invoke ``slidecraft``
subcommands via natural language.

This package itself contains no Python code — it is a data-only sub-package
declared in ``pyproject.toml`` under ``[tool.setuptools.package-data]``.
External packages (e.g. the private ``shuttleslide-pro``) can register
additional skill directories via the ``shuttleslide.skill_assets`` entry-point
group; see :mod:`shuttleslide.extensions.skill_registry`.
"""
