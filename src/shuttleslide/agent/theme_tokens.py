"""Theme placeholder tokens for slide HTML.

Slides reference theme fields via ``{{theme.<field>}}`` placeholders rather
than copying literal values at LLM generation time. This keeps slides bound
to the live theme: changing the theme and re-rendering produces new
colors/fonts without re-invoking the LLM.

Token grammar
-------------
Base form (substitutes the raw field value)::

    {{theme.primary_color}}
    {{theme.accent_color}}
    {{theme.warn_color}}
    {{theme.bg_color}}
    {{theme.text_color}}
    {{theme.title_color}}
    {{theme.font_title}}
    {{theme.font_body}}

Derived form (applies a transform; requires ``:<value>``)::

    {{theme.primary_rgba:0.2}}       # rgba(r, g, b, 0.2)
    {{theme.accent_darken:0.7}}      # darkened hex (#rrggbb)

The alias (e.g. ``primary``) in derived forms is the ThemeDef color field
name without the ``_color`` suffix. Only color fields support derived
forms — fonts do not.

Tokens are substituted at render time
(see :func:`shuttleslide.agent.dsl_to_html.SlideHTMLRenderer.render_slide`)
so neither the browser nor ``html_to_pptx`` ever sees them. The stored
``slots["html"]`` keeps the placeholder version; re-rendering with a new
theme produces new output without touching the slot.
"""

from __future__ import annotations

import re
from typing import Any, List

# Field whitelists — kept in sync with ThemeDef in html_to_pptx/schema.py.
_COLOR_FIELDS = (
    "primary_color",
    "accent_color",
    "warn_color",
    "bg_color",
    "text_color",
    "title_color",
)
_FONT_FIELDS = ("font_title", "font_body")
_ALLOWED_FIELDS = frozenset(_COLOR_FIELDS + _FONT_FIELDS)

# Short aliases for derived forms (primary_color → primary).
_COLOR_ALIASES = {f[: -len("_color")]: f for f in _COLOR_FIELDS}

# Captures: 1=field/alias name, 2=optional :<value>.
# Tolerates surrounding whitespace inside the braces.
_TOKEN_RE = re.compile(
    r"\{\{\s*theme\.([a-zA-Z_]+)(?::([0-9.]+))?\s*\}\}"
)


def _field_value(theme: Any, field: str) -> str:
    """Read a theme field value (handles ThemeDef dataclass or plain dict)."""
    if hasattr(theme, "__dataclass_fields__"):
        return str(getattr(theme, field, ""))
    if isinstance(theme, dict):
        return str(theme.get(field, ""))
    return ""


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """Convert a hex color (with or without leading #) to ``"r, g, b, alpha"``."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return f"0, 0, 0, {alpha}"
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        return f"{r}, {g}, {b}, {alpha}"
    except ValueError:
        return f"0, 0, 0, {alpha}"


def _darken(hex_color: str, factor: float = 0.85) -> str:
    """Darken a hex color by multiplying RGB channels by ``factor``.

    Returns ``#rrggbb``. Used for gradient endpoints (e.g. title bar
    gradient from primary_color to a slightly darker shade of itself).
    """
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return hex_color
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        r = max(0, min(255, int(r * factor)))
        g = max(0, min(255, int(g * factor)))
        b = max(0, min(255, int(b * factor)))
        return f"#{r:02x}{g:02x}{b:02x}"
    except ValueError:
        return hex_color


def _split_alias_modifier(name: str) -> tuple[str, str] | tuple[None, None]:
    """Split ``primary_rgba`` → ``("primary", "rgba")``.

    Returns ``(None, None)`` if the name doesn't end with ``_rgba`` or
    ``_darken``, or if the prefix isn't a known color alias.
    """
    if "_" not in name:
        return (None, None)
    base, _, modifier = name.rpartition("_")
    if modifier in ("rgba", "darken") and base in _COLOR_ALIASES:
        return (base, modifier)
    return (None, None)


def _resolve_token(match: re.Match, theme: Any) -> str:
    """Resolve a single token match to its substituted value.

    Returns the original match text if the token is unknown or malformed.
    :func:`validate_theme_tokens` separately reports these — substitute is
    safe to run on legacy HTML written before the token system existed.
    """
    name = match.group(1)
    arg = match.group(2)
    original = match.group(0)

    # Base form: {{theme.<field>}}
    if arg is None:
        if name in _ALLOWED_FIELDS:
            return _field_value(theme, name)
        return original

    # Derived form: {{theme.<alias>_rgba:<value>}} / {{theme.<alias>_darken:<value>}}
    base, modifier = _split_alias_modifier(name)
    if base is None:
        return original

    raw = _field_value(theme, _COLOR_ALIASES[base])
    try:
        arg_val = float(arg)
    except ValueError:
        return original

    if modifier == "rgba":
        return f"rgba({_hex_to_rgba(raw, arg_val)})"
    if modifier == "darken":
        return _darken(raw, arg_val)
    return original


def substitute_theme_tokens(html: str, theme: Any) -> str:
    """Replace every well-formed ``{{theme.*}}`` token with the live value.

    Unknown / malformed tokens are left in place; see
    :func:`validate_theme_tokens`. Safe on legacy HTML (returns it unchanged).
    """
    if not html or not isinstance(html, str):
        return html
    return _TOKEN_RE.sub(lambda m: _resolve_token(m, theme), html)


def validate_theme_tokens(html: str) -> List[str]:
    """Return error strings for malformed / unknown tokens.

    Empty list means either no tokens present or all tokens well-formed.
    The slide-builder sanitizer calls this and rejects with the first
    error so the LLM retries instead of storing broken markup.
    """
    errors: List[str] = []
    if not html:
        return errors

    for m in _TOKEN_RE.finditer(html):
        name = m.group(1)
        arg = m.group(2)
        token = m.group(0)

        if arg is None:
            if name in _ALLOWED_FIELDS:
                continue
            # Derived form missing :<value>?
            base, modifier = _split_alias_modifier(name)
            if base is not None:
                errors.append(
                    f"token {token!r} requires a :<value> argument "
                    f"(e.g. {{{{theme.{base}_{modifier}:0.5}}}})"
                )
                continue
            errors.append(
                f"unknown token {token!r}; allowed base fields: "
                f"{', '.join(sorted(_ALLOWED_FIELDS))} (plus derived forms "
                f"<alias>_rgba:<alpha> / <alias>_darken:<factor>)"
            )
            continue

        # Has :<value> → must be a derived form.
        base, modifier = _split_alias_modifier(name)
        if base is not None:
            continue
        if name in _ALLOWED_FIELDS:
            errors.append(
                f"token {token!r}: base field {name!r} does not accept a "
                f":<value> argument; only color aliases (primary, accent, "
                f"warn, bg, text, title) do, via _rgba / _darken suffixes"
            )
            continue
        errors.append(
            f"unknown token {token!r}; only color aliases support the "
            f":<value> modifier"
        )
    return errors
