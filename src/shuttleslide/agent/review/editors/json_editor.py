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
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from shuttleslide.agent.review.editors.base import (
    EditResult,
    Editor,
    build_llm_client,
    truncate_for_prompt,
)


@dataclass
class _PathHandler:
    """Resolver for one EditTarget.path.

    The expected type may be ``None`` to defer inference until apply time
    (used by stage_outputs paths where the type is inferred from the
    current value — a list stays a list, a dict stays a dict, a missing
    key defaults to dict).
    """
    expected_type: Optional[type]
    getter: Callable[[Any], Any]
    setter: Callable[[Any, Any], None]
    is_outline: bool = False


# Resolve target.path → handler. Core state attributes are explicit;
# ``("stage_outputs", stage_name, key)`` is a generic pattern so any
# extension stage (subtitle, motion_design, render_video, ...) can edit
# its own JSON-able output without core having to know the stage name.
# The optional 4th+ segments descend into nested dict/list values:
# ``("stage_outputs", "motion_design", "spec", "slides", idx)`` replaces
# only ``state.stage_outputs["motion_design"]["spec"]["slides"][idx]``.
def _resolve_path(path: tuple) -> _PathHandler:
    if path == ("theme",):
        return _PathHandler(
            expected_type=dict,
            getter=lambda s: getattr(s, "theme", {}),
            setter=lambda s, v: setattr(s, "theme", v),
        )
    if path == ("outline",):
        return _PathHandler(
            expected_type=list,
            getter=lambda s: getattr(s, "outline", []),
            setter=lambda s, v: setattr(s, "outline", v),
            is_outline=True,
        )
    if len(path) >= 3 and path[0] == "stage_outputs":
        stage_name, key = path[1], path[2]
        subpath = path[3:]

        def _descend(cur: Any, segments: tuple, *, for_set: bool):
            """Walk ``segments`` into ``cur``. ``for_set`` controls the
            last-segment behavior: getter stops at the last full path
            (returns None if any segment is missing); setter walks to the
            parent and returns ``(parent, last_segment)`` so the caller
            can write. Raises ValueError on type mismatches so the
            editor returns a clean error instead of crashing."""
            if not for_set:
                # Getter path — read-only traversal.
                node = cur
                for seg in segments:
                    if isinstance(seg, int):
                        if not isinstance(node, list) or not (0 <= seg < len(node)):
                            return None
                        node = node[seg]
                    else:
                        if not isinstance(node, dict) or seg not in node:
                            return None
                        node = node[seg]
                return node
            # Setter path — walk to parent, return (parent, last_key).
            if not segments:
                # Should not happen (caller handles whole-value set
                # separately) but be defensive.
                raise ValueError("empty subpath for nested set")
            parent = cur
            for seg in segments[:-1]:
                if isinstance(seg, int):
                    if not isinstance(parent, list) or not (0 <= seg < len(parent)):
                        raise ValueError(
                            f"cannot descend at index {seg}: container is not a list "
                            f"or index out of range"
                        )
                    parent = parent[seg]
                else:
                    if not isinstance(parent, dict) or seg not in parent:
                        raise ValueError(
                            f"cannot descend at key {seg!r}: missing from dict"
                        )
                    parent = parent[seg]
            last = segments[-1]
            if isinstance(last, int):
                if not isinstance(parent, list) or not (0 <= last < len(parent)):
                    raise ValueError(
                        f"list index {last} out of range or container is not a list"
                    )
            else:
                if not isinstance(parent, dict):
                    raise ValueError(
                        f"cannot set key {last!r}: container is not a dict"
                    )
            return parent, last

        def _get(s: Any) -> Any:
            so = getattr(s, "stage_outputs", None) or {}
            sd = so.get(stage_name) or {}
            cur = sd.get(key) if isinstance(sd, dict) else None
            if not subpath:
                return cur
            return _descend(cur, subpath, for_set=False)

        def _set(s: Any, v: Any) -> None:
            so = getattr(s, "stage_outputs", None)
            if so is None:
                s.stage_outputs = {}
                so = s.stage_outputs
            sd = so.get(stage_name)
            if not isinstance(sd, dict):
                sd = {}
                so[stage_name] = sd
            if not subpath:
                # Whole-value replace (original 3-segment path).
                sd[key] = v
                return
            cur = sd.get(key)
            # Container at ``key`` must already exist for nested set —
            # we don't synthesise intermediate structures. JsonEditor
            # callers should rebuild the whole value if the container
            # itself is missing (use the 3-segment form instead).
            if cur is None:
                raise ValueError(
                    f"cannot descend into stage_outputs[{stage_name!r}][{key!r}]: "
                    f"value is missing — edit the parent value instead"
                )
            parent, last = _descend(cur, subpath, for_set=True)
            if isinstance(last, int):
                parent[last] = v
            else:
                parent[last] = v

        # Defer type inference to apply time (see _infer_type below) — the
        # expected type depends on the live value, which isn't known here.
        return _PathHandler(expected_type=None, getter=_get, setter=_set)

    raise ValueError(
        f"JsonEditor does not know which state attribute to write for "
        f"path {tuple(path)!r}; supported patterns: ('theme',), "
        f"('outline',), ('stage_outputs', <stage>, <key>[, <sub>, ...])"
    )


def _infer_expected_type(handler: _PathHandler, state: Any) -> type:
    """For paths whose expected_type is None (stage_outputs), infer from
    the current value. List stays list, dict stays dict, missing or
    scalar defaults to dict (the more common JSON shape)."""
    if handler.expected_type is not None:
        return handler.expected_type
    current = handler.getter(state)
    if isinstance(current, list):
        return list
    return dict


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

    Path resolution lives in ``_resolve_path`` (module-level). Core
    paths (theme, outline) live at top-level state attributes;
    ``("stage_outputs", <stage>, <key>)`` is a generic pattern for
    extension stages (subtitle, motion_design, render_video, ...) so
    each extension can edit its own JSON-able output without core
    having to know the stage name.
    """

    kind = "json"

    def _parse_and_validate(
        self, new_value: str, expected_type: type
    ) -> Any:
        """Parse ``new_value`` as JSON and assert it matches the expected type.

        Empty input is allowed only for list/dict kinds (represents "clear
        this stage" — useful when the user wants to start over without a
        full re-run).
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
        return parsed

    async def apply_direct_edit(
        self, target, new_value, state, config
    ) -> EditResult:
        try:
            handler = _resolve_path(tuple(target.path))
        except ValueError as exc:
            return EditResult(ok=False, error=str(exc))
        expected_type = _infer_expected_type(handler, state)
        try:
            parsed = self._parse_and_validate(new_value, expected_type)
        except ValueError as exc:
            return EditResult(ok=False, error=str(exc))
        if handler.is_outline:
            try:
                _enforce_outline_entry_keys(parsed, state)
            except ValueError as exc:
                return EditResult(ok=False, error=str(exc))
        old_value = handler.getter(state)
        try:
            handler.setter(state, parsed)
        except ValueError as exc:
            # Deep stage_outputs paths validate at set time (list index
            # out of range, missing intermediate container, type
            # mismatch on a nested descent). Surface the error to the
            # caller instead of crashing.
            return EditResult(ok=False, error=str(exc))
        return EditResult(
            ok=True,
            new_value=json.dumps(parsed, ensure_ascii=False, indent=2),
            diff=_unified_diff_json(old_value, parsed),
            assistant_msg=None,
        )

    async def apply_llm_edit(
        self, target, user_message, history, state, config
    ) -> EditResult:
        try:
            handler = _resolve_path(tuple(target.path))
        except ValueError as exc:
            return EditResult(ok=False, error=str(exc))
        expected_type = _infer_expected_type(handler, state)
        current_value = target.current_value or json.dumps(handler.getter(state))
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
                    max_tokens=config.max_tokens,
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

            # Per-entry key check only applies to the real outline path
            # (handler.is_outline). stage_outputs lists (e.g. subtitle
            # slides) have their own per-stage shape and must not be
            # forced through outline's key-preservation rule.
            if handler.is_outline:
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

            old_value = handler.getter(state)
            try:
                handler.setter(state, parsed)
            except ValueError as exc:
                # Deep path validation failure — see apply_direct_edit.
                return EditResult(ok=False, error=str(exc))
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
