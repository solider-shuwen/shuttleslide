"""Editor package — per-element editors for review stage.

Exports:
  * Editor / EditorRegistry / EditResult / default_editors — registry API
  * JsonEditor / SvgEditor / SlideEditor / ImageUploader — concrete editors

Editors mutate AgentState in place; the caller (InteractiveOrchestrator)
handles persistence, undo tracking, and snapshot re-broadcast.
"""

from shuttleslide.agent.review.editors.base import (
    EditResult,
    Editor,
    EditorRegistry,
    build_llm_client,
    default_editors,
    truncate_for_prompt,
)
from shuttleslide.agent.review.editors.image_uploader import ImageUploader
from shuttleslide.agent.review.editors.json_editor import JsonEditor
from shuttleslide.agent.review.editors.slide_editor import SlideEditor
from shuttleslide.agent.review.editors.svg_editor import SvgEditor

__all__ = [
    "EditResult",
    "Editor",
    "EditorRegistry",
    "JsonEditor",
    "SvgEditor",
    "SlideEditor",
    "ImageUploader",
    "default_editors",
    "build_llm_client",
    "truncate_for_prompt",
]
