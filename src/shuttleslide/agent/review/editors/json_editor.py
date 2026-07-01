"""JsonEditor — direct + LLM editing for theme / outline.

Both stages store JSON-able Python structures in AgentState
(``state.theme: dict``, ``state.outline: list[dict]``). The editor
treats them uniformly: serialize the new value to canonical JSON for
state_view, validate it parses back to the same top-level type.

LLM mode
--------
No tool definition — the system prompt embeds the current value and
asks for a complete replacement. JSON is permissive enough that tool
schemas would over-constrain (e.g. theme shape is decided by the LLM
itself during stage 1). Parsing the response as JSON catches
malformed output; we then type-check against the original.
"""

from __future__ import annotations

import difflib
import json
from typing import Any, Dict, List, Optional

from shuttleslide.agent.review.editors.base import (
    EditResult,
    Editor,
    build_llm_client,
    truncate_for_prompt,
)

# Map target.path → (state attribute, expected top-level type).
# Keeping this in one place makes it easy to extend (e.g. future
# stage_outputs edits could add their own entry here).
_PATH_TO_STATE: Dict = {
    ("theme",): ("theme", dict),
    ("outline",): ("outline", list),
}


# How many times apply_llm_edit will retry after a JSON parse / type
# error before giving up. Same value as slide_editor's
# _LLM_EDIT_MAX_RETRIES so all LLM-aware editors share the same retry
# budget — a theme edit and a slide HTML edit can both fail in similar
# ways (LLM wraps output in a code fence, drops a trailing comma,
# returns prose around the JSON, ...).
# Total attempts = 1 + _LLM_EDIT_MAX_RETRIES = 4.
_LLM_EDIT_MAX_RETRIES = 3


class JsonEditor(Editor):
    """Editor for ``kind="json"`` targets.

    ``state_attr`` is determined from ``target.path`` — theme and outline
    are the only json-kind targets in the core pipeline, and they live
    at well-known state attributes.
    """

    kind = "json"

    def _resolve_state_attr(self, target) -> str:
        path = tuple(target.path)
        if path not in _PATH_TO_STATE:
            raise ValueError(
                f"JsonEditor does not know which state attribute to write for "
                f"path {path!r}; supported paths: {list(_PATH_TO_STATE.keys())}"
            )
        attr, _expected_type = _PATH_TO_STATE[path]
        return attr

    def _parse_and_validate(
        self, new_value: str, expected_type: type, *, state: Any = None
    ) -> Any:
        """Parse ``new_value`` as JSON and assert it matches the expected type.

        Empty input is allowed only for list/dict kinds (represents "clear
        this stage" — useful when the user wants to start over without a
        full re-run).

        When ``expected_type is list`` and ``state.outline`` already has
        entries, additionally enforce per-entry key preservation: every
        new entry must be a dict whose key set matches the corresponding
        old entry's key set (ignoring ``_detail_filled`` which the form
        omits). This is the server-side backstop against UI bugs that
        would silently drop or rename an outline field. The structured
        outline editor already enforces this in the form shape, but a
        defensive check here means a hand-crafted WS payload can't
        corrupt the schema either.
        """
        text = (new_value or "").strip()
        if not text:
            if expected_type is dict:
                return {}
            if expected_type is list:
                return []
            raise ValueError("value is empty")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"value is not valid JSON: {exc}") from exc
        if not isinstance(parsed, expected_type):
            raise ValueError(
                f"value must be a {expected_type.__name__}, got "
                f"{type(parsed).__name__}"
            )
        if expected_type is list:
            _enforce_outline_entry_keys(parsed, state)
        return parsed

    async def apply_direct_edit(
        self, target, new_value, state, config
    ) -> EditResult:
        attr = self._resolve_state_attr(target)
        _, expected_type = _PATH_TO_STATE[tuple(target.path)]
        try:
            parsed = self._parse_and_validate(
                new_value, expected_type, state=state
            )
        except ValueError as exc:
            return EditResult(ok=False, error=str(exc))
        old_value = getattr(state, attr)
        setattr(state, attr, parsed)
        return EditResult(
            ok=True,
            new_value=json.dumps(parsed, ensure_ascii=False, indent=2),
            diff=_unified_diff_json(old_value, parsed),
            assistant_msg=None,
        )

    async def apply_llm_edit(
        self, target, user_message, history, state, config
    ) -> EditResult:
        attr = self._resolve_state_attr(target)
        _, expected_type = _PATH_TO_STATE[tuple(target.path)]
        current_value = target.current_value or json.dumps(getattr(state, attr))
        llm = build_llm_client(config)
        system_prompt = (
            f"You are revising the JSON for the {target.stage!r} stage.\n"
            f"Current value:\n\n```json\n{truncate_for_prompt(current_value)}\n```\n\n"
            f"Apply the user's requested change and return the COMPLETE new "
            f"JSON object — not a diff, not an explanation. Output must be "
            f"valid JSON parseable by ``json.loads`` with no surrounding prose "
            f"and no code fences."
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # Retry loop: feed JSON parse / type errors back to the LLM so
        # it can correct the output (drop prose, remove code fence, fix
        # trailing comma, swap a list for a dict, ...). Mirrors
        # slide_editor.apply_llm_edit's retry pattern. Network errors
        # still short-circuit — only parse / type failures retry.
        # JsonEditor has no tool_call_id (no tools defined), so the
        # feedback message uses role="user" instead of role="tool".
        last_error: Optional[str] = None
        for _ in range(_LLM_EDIT_MAX_RETRIES + 1):
            try:
                resp = await llm.chat_with_tools(
                    messages=messages,
                    tools=None,
                    temperature=max(0.0, min(1.0, config.temperature)),
                    max_tokens=config.max_tokens or 4096,
                )
            except Exception as exc:
                return EditResult(ok=False, error=f"LLM call failed: {exc}")

            content = (resp.content or "").strip()
            # Strip stray code fences if the model wraps despite the instruction.
            if content.startswith("```"):
                content = _strip_code_fence(content)
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                # Push the bad response back as an assistant turn so the
                # next iteration sees what failed, then a user nudge to
                # re-emit clean JSON. role="user" is fine here because
                # we never called a tool (no tool_call_id to satisfy).
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Previous response was not valid JSON: {exc.msg}. "
                        f"Output ONLY the JSON object, no prose, no code "
                        f"fences, no trailing commas."
                    ),
                })
                last_error = f"value is not valid JSON: {exc.msg}"
                continue
            if not isinstance(parsed, expected_type):
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Previous response was a {type(parsed).__name__}, "
                        f"but the {target.stage!r} stage requires a "
                        f"{expected_type.__name__}. Output ONLY the complete "
                        f"{expected_type.__name__} as valid JSON."
                    ),
                })
                last_error = (
                    f"value must be a {expected_type.__name__}, got "
                    f"{type(parsed).__name__}"
                )
                continue

            # Per-entry key check (no-op for non-list kinds / empty state).
            try:
                _enforce_outline_entry_keys(parsed, state)
            except ValueError as exc:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Previous response changed the outline entry keys: "
                        f"{exc}. Every slide entry must keep the same key set "
                        f"as the existing outline (only ``_detail_filled`` "
                        f"may be omitted). Output the JSON again with the "
                        f"original keys preserved."
                    ),
                })
                last_error = str(exc)
                continue

            old_value = getattr(state, attr)
            setattr(state, attr, parsed)
            # assistant_msg is the chat-visible reply, not the raw JSON.
            # The system prompt forbids prose in the LLM response to
            # keep JSON parsing reliable, so resp.content is the full
            # JSON object — useless as a chat line. Synthesize a short
            # description from the stage name + the user's request
            # instead.
            msg_preview = (user_message or "").strip()
            if len(msg_preview) > 80:
                msg_preview = msg_preview[:77] + "..."
            assistant_msg = (
                f"Updated {target.stage}: {msg_preview}"
                if msg_preview
                else f"Updated {target.stage}."
            )
            return EditResult(
                ok=True,
                new_value=json.dumps(parsed, ensure_ascii=False, indent=2),
                diff=_unified_diff_json(old_value, parsed),
                assistant_msg=assistant_msg,
            )

        return EditResult(
            ok=False,
            error=(
                f"After {_LLM_EDIT_MAX_RETRIES + 1} attempts the LLM still "
                f"produced invalid JSON. Last error: {last_error}"
            ),
        )


def _strip_code_fence(text: str) -> str:
    """Remove a single wrapping ```...``` (with optional language tag)."""
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


# Keys the structured outline editor renders. ``_detail_filled`` is a
# pipeline-internal progress flag (set by ``run_slide_detail_generator``)
# and the form doesn't show it — its absence in a commit payload is
# legitimate and must not trip the key-preservation check. Adding it
# back on the server side is the orchestrator's job, not the editor's.
_OUTLINE_OPTIONAL_KEYS = {"_detail_filled"}


def _enforce_outline_entry_keys(parsed: Any, state: Any) -> None:
    """Reject outline edits that change per-entry key sets.

    Active only when ``parsed`` is a list AND ``state.outline`` already
    has entries to compare against. Empty state (first edit) skips the
    check — there is no baseline to enforce yet.

    Per-entry rules:
      - every new entry must be a dict
      - if a same-position old entry exists, the key set must match
        modulo ``_detail_optional_keys``
      - extra entries (append) have no baseline, so any dict is accepted
    """
    if not isinstance(parsed, list):
        return
    old_outline = getattr(state, "outline", None) if state is not None else None
    if not old_outline:
        return
    for i, new_entry in enumerate(parsed):
        if not isinstance(new_entry, dict):
            raise ValueError(
                f"outline entry {i} must be an object, got "
                f"{type(new_entry).__name__}"
            )
        if i >= len(old_outline):
            continue  # appended entry; no baseline
        old_entry = old_outline[i]
        if not isinstance(old_entry, dict):
            continue  # corrupt baseline; don't false-positive
        old_keys = {
            k for k in old_entry.keys() if k not in _OUTLINE_OPTIONAL_KEYS
        }
        new_keys = {
            k for k in new_entry.keys() if k not in _OUTLINE_OPTIONAL_KEYS
        }
        if old_keys != new_keys:
            missing = sorted(old_keys - new_keys)
            extra = sorted(new_keys - old_keys)
            parts = []
            if missing:
                parts.append(f"missing keys: {missing}")
            if extra:
                parts.append(f"extra keys: {extra}")
            raise ValueError(
                f"outline entry {i} key set changed ({'; '.join(parts)})"
            )


def _unified_diff_json(old, new) -> str:
    """Human-readable unified diff of two JSON-serialisable values."""
    old_text = json.dumps(old, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    new_text = json.dumps(new, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    diff = list(
        difflib.unified_diff(old_text, new_text, lineterm="", n=2)
    )
    return "\n".join(diff) if diff else ""
