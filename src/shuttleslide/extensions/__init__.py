"""Extension points for third-party / commercial packages.

Shuttleslide exposes a small set of entry-point groups so external packages
can plug into the CLI without changes to this repo:

- ``shuttleslide.cli_commands`` — Click subcommands added to ``slidecraft``.
  Each entry point must resolve to a ``click.Command`` (typically created
  with ``@click.command()``). Loaded once at process start by
  :func:`shuttleslide.extensions.cli_registry.register_extensions`.

See ``CLAUDE.md`` ("Extension mechanism") for the full authoring guide.
"""

from shuttleslide.extensions.cli_registry import register_extensions  # noqa: F401

__all__ = ["register_extensions"]
