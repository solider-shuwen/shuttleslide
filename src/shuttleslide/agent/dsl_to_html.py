"""DSL → HTML renderer (free-form HTML wrapper).

Renders PresentationDSL / SlideDSL into standalone HTML documents:
  - Tailwind CDN + Roboto + Noto Sans SC + Material Icons in <head>
  - .ppt-slide container at 1280x720
  - The slide's inner HTML (authored by the LLM) is wrapped by the
    free_form template

This is the deterministic Stage 4 of the pipeline — no LLM involvement.
The LLM-facing `set_free_form_html` tool writes the inner HTML into
`slide.slots["html"]`; this renderer wraps it in the outer document.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import jinja2

from shuttleslide.agent.theme_tokens import (
    _darken,
    _hex_to_rgba,
    substitute_theme_tokens,
)
from shuttleslide.html_to_pptx.schema import (
    PresentationDSL,
    SlideDSL,
    ThemeDef,
)


# Pure free-form pipeline: every slide is wrapped by free_form.html.j2.
# Kept as a set for backwards-compatible shape; only "free_form" is valid.
VALID_LAYOUTS = {"free_form"}
_DEFAULT_LAYOUT = "free_form"


# Matches the first <h1>...</h1> or <h2>...</h2> in the slide's HTML.
# Used to derive a sensible slide title for the document <title>.
_HEADING_RE = re.compile(r"<h[12][^>]*>(.*?)</h[12]>", re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------------------
# Theme color helpers — _hex_to_rgba and _darken live in theme_tokens.py
# (shared with the {{theme.*}} placeholder substitution path). The wrappers
# below are presentation-specific (they map "primary"/"accent"/"warn"
# keywords to theme fields) and stay here, registered as Jinja globals so
# the free_form template can call them directly.
# ---------------------------------------------------------------------------


def _theme_color(theme: Any, accent: str) -> str:
    """Map an accent keyword ('primary'|'accent'|'warn') to the theme's hex color.

    `theme` may be a ThemeDef dataclass or a plain dict (the agent stores
    theme as a dict in `state.theme`).
    """
    if hasattr(theme, "__dataclass_fields__"):
        # ThemeDef dataclass instance
        colors = {
            "primary": theme.primary_color,
            "accent": theme.accent_color,
            "warn": getattr(theme, "warn_color", "#FF5722"),
        }
    else:
        # Plain dict from state.theme
        colors = {
            "primary": theme.get("primary_color", "#133EFF"),
            "accent": theme.get("accent_color", "#00CD82"),
            "warn": theme.get("warn_color", "#FF5722"),
        }
    return colors.get(accent or "primary", colors["primary"])


def _accent_rgba(theme: Any, accent: str, alpha: float) -> str:
    """Return rgba(...) string for a theme accent at a given alpha."""
    return f"rgba({_hex_to_rgba(_theme_color(theme, accent), alpha)})"


# Cross-platform system font stack. Used as the default body/title font
# family. The theme designer may specify a preferred font (e.g. "Inter"
# or "Roboto"); it gets prepended to this stack so the browser uses it
# if installed, and falls back to system fonts otherwise.
#
# Why a system stack and not Roboto/Noto Sans SC: see cdn_assets.py —
# inlining Google Fonts produces a ~47 MB CSS file. System fonts on
# Win/Mac/Linux are all high-quality sans-serif, and Chinese system
# fonts (PingFang SC, Microsoft YaHei) are actually higher quality
# than embedded Noto Sans SC at small sizes.
_SYSTEM_FONT_STACK = (
    '"Inter", "Roboto", -apple-system, BlinkMacSystemFont, "Segoe UI", '
    '"PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", sans-serif'
)


def _font_stack(theme_font: Optional[str]) -> str:
    """Build a CSS font-family value with `theme_font` prepended to the system stack.

    If `theme_font` is None or empty, returns the bare system stack.
    """
    if not theme_font:
        return _SYSTEM_FONT_STACK
    return f'"{theme_font}", {_SYSTEM_FONT_STACK}'


class SlideHTMLRenderer:
    """Renders PresentationDSL / SlideDSL into HTML."""

    def __init__(self, inline_cdn_assets: bool = True) -> None:
        """Initialise the renderer.

        Args:
            inline_cdn_assets: When True (default), the renderer downloads
                Tailwind, Google Fonts, and Material Icons on first use and
                inlines them into the HTML so the document works offline.
                Set to False to keep the original CDN <script>/<link> tags
                (useful for tests or environments that intentionally want
                the live CDN).
        """
        self.inline_cdn_assets = inline_cdn_assets
        self.env = jinja2.Environment(
            loader=jinja2.PackageLoader("shuttleslide.agent", "templates"),
            autoescape=jinja2.select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # Register utility functions visible to templates.
        self.env.globals["_hex_to_rgba"] = _hex_to_rgba
        self.env.globals["_theme_color"] = _theme_color
        self.env.globals["_accent_rgba"] = _accent_rgba
        self.env.globals["_darken"] = _darken

    # -- public API -------------------------------------------------------

    def render_presentation(
        self,
        pres: PresentationDSL,
        title: Optional[str] = None,
        canvas_width_emu: Optional[int] = None,
        canvas_height_emu: Optional[int] = None,
    ) -> List[str]:
        """Render every slide as a standalone HTML document."""
        # Fall back to the presentation's own dimensions when caller didn't
        # override (e.g. html_to_pptx flow that loads a JSON file).
        w_emu = canvas_width_emu if canvas_width_emu is not None else pres.slide_width_emu
        h_emu = canvas_height_emu if canvas_height_emu is not None else pres.slide_height_emu
        out: List[str] = []
        for i, slide in enumerate(pres.slides, start=1):
            slide_title = title or f"Slide {i}"
            # Per-slide fallback: extract the first <h1>/<h2> from the
            # authored HTML to use as the document title.
            if not title:
                html = slide.slots.get("html", "")
                if isinstance(html, str):
                    m = _HEADING_RE.search(html)
                    if m:
                        # Strip nested tags from the heading text.
                        heading_text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                        if heading_text:
                            slide_title = heading_text[:80]
            out.append(
                self.render_slide(
                    slide, pres.theme, slide_title,
                    canvas_width_emu=w_emu, canvas_height_emu=h_emu,
                )
            )
        return out

    def render_slide(
        self,
        slide: SlideDSL,
        theme: ThemeDef,
        title: str = "Slide",
        canvas_width_emu: Optional[int] = None,
        canvas_height_emu: Optional[int] = None,
    ) -> str:
        """Render a single slide as a standalone HTML document."""
        layout_name = slide.layout if slide.layout in VALID_LAYOUTS else _DEFAULT_LAYOUT
        # Substitute {{theme.*}} placeholders in the authored HTML before
        # rendering. The slot stores the placeholder version (so the same
        # slide re-renders cleanly when theme changes); only the rendered
        # output carries the live values. Legacy HTML written before the
        # token system has no placeholders and passes through unchanged.
        slots = dict(slide.slots)
        raw_html = slots.get("html")
        if isinstance(raw_html, str):
            slots["html"] = substitute_theme_tokens(raw_html, theme)
        slide_html = self.env.get_template(f"layouts/{layout_name}.html.j2").render(
            slots=slots,
            theme=theme,
        )
        # CDN assets — downloaded once and cached at ~/.shuttleslide/cdn/.
        # None means "download failed and no cache exists"; the template
        # then falls back to the live CDN URL.
        #
        # Note: Google Fonts is intentionally NOT inlined here. The Google
        # Fonts CSS embeds full Noto Sans SC TTFs (~10 MB per weight) when
        # our urllib User-Agent is sent, producing a ~47 MB CSS file. The
        # template uses a cross-platform system font stack instead. Use
        # cdn_assets.get_google_fonts_css() directly if you need an opt-in.
        from shuttleslide.agent import cdn_assets
        tailwind_script: Optional[str] = None
        material_icons_css: Optional[str] = None
        if self.inline_cdn_assets:
            tailwind_script = cdn_assets.get_tailwind_script()
            material_icons_css = cdn_assets.get_material_icons_css()
        # Canvas dimensions in CSS px (96 DPI). Defaults reproduce 16:9.
        # Late import keeps the module-load dependency graph clean.
        from shuttleslide.agent.geometry import EMU_PER_CSS_PX
        canvas_width_px = (
            canvas_width_emu // EMU_PER_CSS_PX if canvas_width_emu else 1280
        )
        canvas_height_px = (
            canvas_height_emu // EMU_PER_CSS_PX if canvas_height_emu else 720
        )
        return self.env.get_template("presentation.html.j2").render(
            title=title,
            slide_body=slide_html,
            title_font=_font_stack(theme.font_title),
            body_font=_font_stack(theme.font_body),
            body_background=theme.bg_color or "#0a0e27",
            tailwind_script=tailwind_script,
            material_icons_css=material_icons_css,
            tailwind_cdn_url=cdn_assets.DEFAULT_TAILWIND_URL,
            material_icons_cdn_url=cdn_assets.DEFAULT_MATERIAL_ICONS_URL,
            canvas_width_px=canvas_width_px,
            canvas_height_px=canvas_height_px,
        )

    def render_slides_to_files(
        self,
        pres: PresentationDSL,
        output_dir: Path,
        title_prefix: Optional[str] = None,
        canvas_width_emu: Optional[int] = None,
        canvas_height_emu: Optional[int] = None,
    ) -> List[Path]:
        """Render and write each slide as 1.html, 2.html, ... in output_dir."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        htmls = self.render_presentation(
            pres, title=title_prefix,
            canvas_width_emu=canvas_width_emu,
            canvas_height_emu=canvas_height_emu,
        )
        written: List[Path] = []
        for i, html in enumerate(htmls, start=1):
            path = output_dir / f"{i}.html"
            path.write_text(html, encoding="utf-8")
            written.append(path)
        return written
