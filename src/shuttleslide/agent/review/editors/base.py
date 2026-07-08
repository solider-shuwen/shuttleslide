"""Editor Protocol + EditResult + EditorRegistry.

Per-element editors are PR3's contribution on top of PR2's stage-level
review. The flow:

    request_edit (WS)
        → server resolves EditTarget from current snapshot
        → InteractiveOrchestrator.apply_edit(target, mode, payload)
        → editor = registry.get(target.kind)
        → editor.apply_direct_edit / apply_llm_edit / image upload
        → editor mutates AgentState in place
        → orchestrator saves state, rebuilds snapshot, re-broadcasts

Editors never touch the broadcaster or the undo stack directly —
``InteractiveOrchestrator.apply_edit`` wraps the editor call with undo
tracking, persistence, and snapshot re-emit. The editor's only job is
"given a target + new value (or LLM instruction), mutate state and
report success/failure."

Cross-loop dispatch
-------------------
``apply_edit`` lives on the orchestrator and is called from the server
loop. In web-client mode both loops are the same (orchestrator runs on
the server's loop via POST /api/start), so direct ``await`` is safe. In
legacy mode (CLI studio), the orchestrator loop is separate and edits
must dispatch via ``loop.call_soon_threadsafe`` — but PR3 doesn't
implement that path; WS handlers return "edit not available in
external-orchestrator mode" instead.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from shuttleslide.agent.config import AgentConfig
    from shuttleslide.agent.review.review_gate import EditTarget
    from shuttleslide.agent.state import AgentState


# Public, stable entry-point group name. External packages (e.g.
# shuttleslide-pro's MotionChatEditor) register additional Editor
# subclasses here; default_editors() discovers and loads them after the
# 4 built-ins. Mirrors shuttleslide.cli_commands / shuttleslide.review.*
# extension groups — see CLAUDE.md "Extension mechanism".
EDITOR_ENTRY_POINT_GROUP = "shuttleslide.review.editors"


@dataclass
class EditResult:
    """Outcome of an editor.apply_*_edit call.

    On success (``ok=True``):
      * ``new_value`` is the canonical value actually written to state
        (may differ from input — e.g. re-formatted JSON, re-encoded JPEG).
        Used for undo stack entries and chat-history display.
      * ``assistant_msg`` carries the LLM's user-visible reply in
        ``apply_llm_edit`` mode (typically a one-line ack or explanation).
        Empty for ``apply_direct_edit``.
      * ``diff`` is an optional unified diff for chat display.
      * ``no_op`` signals that ``new_value`` equalled the live value
        when apply_edit ran, so no UndoStack entry was pushed and no
        stage_complete re-broadcast is warranted. The server sends an
        ``EditAppliedMsg`` with ``no_op=True`` (rather than silently
        skipping) so clients can clear any pending chat indicator
        without flipping the "edited" flag.

    On failure (``ok=False``):
      * ``error`` is a user-facing error message (English prose; no
        stack traces, no internal ids).
      * ``new_value`` / ``diff`` / ``assistant_msg`` should be None.

    Out-of-scope rejection (``ok=False``, ``kind="out_of_scope"``):
      * The LLM recognised the request as deck-level (adds/removes
        slides, changes theme, restructures outline) and called
        ``reject_request`` instead of mutating the per-slide target.
        ``suggested_stage`` is the stage the user should switch to
        (typically "outline"); ``guidance`` is the chat-visible text.
        The server routes this through ``EditRejectedMsg`` so the UI
        can render a guidance card with a stage-switch button instead
        of a plain error.
    """

    ok: bool
    new_value: Optional[str] = None
    error: Optional[str] = None
    diff: Optional[str] = None
    assistant_msg: Optional[str] = None
    no_op: bool = False
    # Decoded pixel dimensions of the uploaded image, when ``new_value``
    # is an image path. None for text/JSON/SVG/HTML edits. Used by the
    # slides-stage drag-drop feature so the client can size a newly
    # inserted <img> with the correct aspect ratio without a second
    # round-trip. See ImageUploader._apply for the only producer.
    width: Optional[int] = None
    height: Optional[int] = None
    # Description actually written to state for an uploaded image
    # (user-supplied, VLM-generated, or empty when both paths failed /
    # were disabled). None for non-image edits. Surfaced back to the
    # client so the upload ack can show what landed in state without
    # the client having to refetch the snapshot. See ImageUploader._apply.
    description: Optional[str] = None
    # Discriminator for failure routing. "error" (default) is the plain
    # rejection the UI has always rendered. "out_of_scope" means the
    # request was deck-level and the editor returned structured guidance
    # instead of an error string — the UI shows a switch-stage card.
    kind: str = "error"
    # Stage the user should navigate to when kind="out_of_scope"
    # (e.g. "outline"). None for normal errors.
    suggested_stage: Optional[str] = None
    # User-facing guidance text shown in the chat card when
    # kind="out_of_scope". Should explain *why* the request was rejected
    # and what to do next. None for normal errors.
    guidance: Optional[str] = None


class Editor(ABC):
    """Per-kind editor. Concrete editors register via ``EditorRegistry``.

    ``kind`` is the dispatch key — matches ``EditTarget.kind``. One
    editor instance per kind lives in the registry for the pipeline's
    lifetime; per-edit state is passed via method args (target / history).
    """

    kind: str

    @abstractmethod
    async def apply_direct_edit(
        self,
        target: "EditTarget",
        new_value: str,
        state: "AgentState",
        config: "AgentConfig",
    ) -> EditResult:
        """Apply a user-supplied replacement value verbatim.

        Validation runs the same code path as the pipeline — direct
        edits are NOT a shortcut that bypasses checks. This is why
        ``validate_svg_for_slot`` and ``_validate_free_form_html`` were
        extracted to pure functions in Step 1 / existing slide_tools.
        """
        raise NotImplementedError

    @abstractmethod
    async def apply_llm_edit(
        self,
        target: "EditTarget",
        user_message: str,
        history: List[Dict[str, str]],
        state: "AgentState",
        config: "AgentConfig",
    ) -> EditResult:
        """Ask the LLM to revise the target per ``user_message``.

        ``history`` is the prior chat for THIS target.path (from
        ``SessionStore``). Editors embed ``target.current_value`` in the
        system prompt each call — it's not preloaded into history. Only
        successful LLM edits get appended to history by the orchestrator.
        """
        raise NotImplementedError


def build_llm_client(config: "AgentConfig"):
    """Construct an LLMClient mirroring the orchestrator's setup.

    Editors don't share the orchestrator's LLMClient instance — that
    keeps them decoupled from InteractiveOrchestrator and prevents a
    stuck editor call from blocking pipeline-side requests. ``LLMClient``
    is cheap to construct (the underlying openai client is lazy-built
    on first call), so per-edit-instance construction is fine.
    """
    from shuttleslide.agent.llm.client import LLMClient

    return LLMClient(
        api_base=config.api_base,
        api_key=config.api_key,
        model=config.model,
        disable_required_tool_choice=config.disable_required_tool_choice,
    )


def truncate_for_prompt(text: str, limit: int = 8000) -> str:
    """Truncate large payloads before embedding in an LLM prompt.

    SVG / HTML / JSON can be several KB; if the LLM is meant to revise
    the whole thing it must fit in context. We keep the head (most
    structural info is there) and append a truncation marker so the
    model knows it's not seeing the entire value. The full original
    still lives in ``EditTarget.current_value`` for display.
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text)} chars total)"


class EditorRegistry:
    """kind → Editor mapping.

    Lookup is by ``EditTarget.kind``. Unknown kinds return None at the
    registry level; ``InteractiveOrchestrator.apply_edit`` turns that
    into an EditResult error so the user sees a useful message.
    """

    def __init__(self) -> None:
        self._editors: Dict[str, Editor] = {}

    def register(self, editor: Editor) -> None:
        if not editor.kind:
            raise ValueError("editor.kind must be a non-empty string")
        if editor.kind in self._editors:
            raise ValueError(f"editor for kind {editor.kind!r} already registered")
        self._editors[editor.kind] = editor

    def get(self, kind: str) -> Optional[Editor]:
        return self._editors.get(kind)

    def kinds(self) -> List[str]:
        return sorted(self._editors.keys())


def _iter_editor_entry_points():
    """Yield entry points in the ``shuttleslide.review.editors`` group.

    Wrapper around ``importlib.metadata.entry_points`` so tests can
    monkeypatch one place. Same shape as
    :func:`shuttleslide.extensions.cli_registry.iter_extension_entry_points`.
    """
    try:
        # Python 3.10+ returns a SelectableGroups; entry_points(group=...)
        # is the stable selector across 3.9–3.12.
        return entry_points(group=EDITOR_ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — very old Python shape
        return entry_points().get(EDITOR_ENTRY_POINT_GROUP, [])


def _register_extension_editors(registry: EditorRegistry) -> None:
    """Discover and register Editor subclasses from external packages.

    Iterates ``shuttleslide.review.editors`` entry points. Each entry
    point may resolve to either:

    * an ``Editor`` subclass — instantiated with no args, then registered
    * an ``Editor`` instance — registered directly (lets extensions
      inject config / collaborators at construction time)

    Failure isolation mirrors :func:`shuttleslide.extensions.cli_registry.register_extensions`:

    * Entry point fails to load → log to stderr, continue.
    * Loaded value is not a class/instance of ``Editor`` → log, skip.
    * Extension's ``kind`` collides with a built-in (or previously
      registered extension) → log, skip. Built-ins always win; first
      extension to claim a kind wins over later ones.

    No-op when no extensions are registered (vanilla installs).
    """
    import inspect

    for ep in _iter_editor_entry_points():
        try:
            loaded = ep.load()
        except Exception as exc:  # noqa: BLE001 — extension isolation
            print(
                f"[shuttleslide] failed to load editor extension "
                f"'{ep.name}' from {ep.value}: {exc}",
                file=sys.stderr,
            )
            continue

        # Accept class or instance. Class → instantiate; instance → use as-is.
        if inspect.isclass(loaded):
            if not issubclass(loaded, Editor):
                print(
                    f"[shuttleslide] editor extension '{ep.name}' class "
                    f"{loaded!r} is not an Editor subclass; skipping.",
                    file=sys.stderr,
                )
                continue
            try:
                editor = loaded()
            except Exception as exc:  # noqa: BLE001 — extension isolation
                print(
                    f"[shuttleslide] editor extension '{ep.name}' failed "
                    f"to instantiate: {exc}",
                    file=sys.stderr,
                )
                continue
        elif isinstance(loaded, Editor):
            editor = loaded
        else:
            print(
                f"[shuttleslide] editor extension '{ep.name}' loaded "
                f"{loaded!r} which is neither an Editor class nor "
                f"instance; skipping.",
                file=sys.stderr,
            )
            continue

        if editor.kind in registry._editors:  # type: ignore[attr-defined]
            print(
                f"[shuttleslide] editor extension '{ep.name}' kind "
                f"{editor.kind!r} already registered; skipping.",
                file=sys.stderr,
            )
            continue

        try:
            registry.register(editor)
        except ValueError as exc:  # paranoia — race-free recheck above
            print(
                f"[shuttleslide] editor extension '{ep.name}' register "
                f"failed: {exc}",
                file=sys.stderr,
            )
            continue


def default_editors() -> EditorRegistry:
    """Return a fresh registry pre-populated with the 4 built-in editors.

    Returns a NEW registry each call so callers can mutate it (e.g. swap
    in a stub for testing) without affecting other orchestrator instances.
    Lazy imports keep ``editors/__init__`` importable without Pillow /
    python-multipart installed — JsonEditor / SvgEditor / SlideEditor
    only need the core deps; ImageUploader pulls in Pillow.

    After the built-ins are registered, ``shuttleslide.review.editors``
    entry points are discovered and registered — external packages
    (e.g. ``shuttleslide-pro``'s ``MotionChatEditor``) plug in here.
    Built-in kinds always win; an extension that re-declares ``"json"``
    is skipped. See CLAUDE.md "Extension mechanism".
    """
    from shuttleslide.agent.review.editors.image_uploader import ImageUploader
    from shuttleslide.agent.review.editors.json_editor import JsonEditor
    from shuttleslide.agent.review.editors.slide_editor import SlideEditor
    from shuttleslide.agent.review.editors.svg_editor import SvgEditor

    registry = EditorRegistry()
    registry.register(JsonEditor())
    registry.register(SvgEditor())
    registry.register(SlideEditor())
    registry.register(ImageUploader())
    _register_extension_editors(registry)
    return registry
