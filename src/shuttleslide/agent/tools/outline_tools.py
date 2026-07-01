"""Outline Planner tool — single atomic tool that defines the slide outline."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from shuttleslide.agent.tools.registry import ToolResult, tool

# Hard bounds for the deck length. Pyramid structure needs at least
# Overview + content + Synthesis (3 slides); 30 is a practical ceiling
# driven by LLM context budget and viewer attention span. The outline
# planner prompt also states soft targets (6-15 typical, 25 for dense
# reference material); these hard bounds exist to catch LLM drift and
# trigger the retry loop in run_outline_planner.
MIN_SLIDES = 3
MAX_SLIDES = 30

# snake_case slot id: starts with lowercase letter, then lowercase /
# digits / underscores. Mirrors the `pattern` in the JSON schema below
# (which is documentation only — OpenAI function calling doesn't
# enforce `pattern`).
_SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Hard bounds per slide for image specs. Each image becomes an LLM call
# in Stage 2.5 (SVG Generator) plus a chunk of slide HTML in Stage 3,
# so we cap density to keep latency and HTML weight bounded.
MAX_IMAGES_PER_SLIDE = 3

# Aspect ratios the SVG Generator knows how to map to a viewBox. Keep
# in sync with _ASPECT_VIEWBOX in prompts.py and the SVG_GENERATOR_PROMPT.
ALLOWED_ASPECT_RATIOS = ("16:9", "4:3", "1:1", "3:2", "2:3")

# Image categories the SVG Generator knows how to draw. The prompt
# gives explicit guidance per category; unknown values are rejected at
# validation time so the LLM retries instead of producing off-target art.
# `hero` is split out from `illustration` so downstream stages can decide
# placement/visual-weight by INTENT (full-bleed cover art vs spot deco).
ALLOWED_IMAGE_TYPES = ("hero", "flowchart", "diagram", "illustration", "icon_cluster", "chart")

# Where the image comes from. The default "svg" preserves the historical
# behaviour (LLM generates inline SVG → converted to native PPT shapes).
# "web" routes the spec through the image acquirer's web path (image
# search API → download → VLM verification), with fallback to "svg" if
# acquisition or VLM verification fails. "user_upload" pulls from the
# user_image_library the user pre-staged on the homepage — the outline
# planner is REQUIRED to use every library entry before falling back to
# svg/web for remaining slots. Kept distinct from `image_type` because
# the same `image_type` (e.g. hero) can come from either source.
ALLOWED_SOURCE_TYPES = ("svg", "web", "user_upload")

# When source_type="web", source_ref may be a http(s) URL or a free-text
# search query. URL values route through playwright screenshot; query
# values route through the configured image search provider.
_SOURCE_REF_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


@tool(
    name="define_outline",
    description=(
        "Define the slide-by-slide outline for the deck. Call this ONCE with "
        "a list of slides. Each slide has title, purpose, key_points, "
        "layout_hint (a free-text description of the desired visual structure), "
        "and optionally images (0-3 specs). Each image spec declares a "
        "source_type: 'svg' (LLM-generated inline SVG → editable native PPT "
        "shapes), 'web' (fetched from a web image search and verified by a "
        "VLM), or 'user_upload' (from the user's pre-staged image library — "
        "REQUIRED to use every library entry before falling back to svg/web). "
        "source_type='web' / 'user_upload' require source_ref."
    ),
    params={
        "type": "object",
        "properties": {
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "purpose": {
                            "type": "string",
                            "description": "One sentence: what role this slide plays in the deck.",
                        },
                        "key_points": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "2-5 short bullets — the content this slide must communicate.",
                        },
                        "layout_hint": {
                            "type": "string",
                            "description": (
                                "Free-text description of the desired visual structure. "
                                "Be specific enough that a designer reading just the hint "
                                "could sketch the slide. Examples: 'Full-bleed hero cover "
                                "with big icon and 3 tech tag pills at the bottom', "
                                "'Title bar at top, 2-column grid with a card on the left "
                                "and a vertical numbered list of 4 steps on the right'."
                            ),
                        },
                        "images": {
                            "type": "array",
                            "maxItems": MAX_IMAGES_PER_SLIDE,
                            "description": (
                                "Optional 0-3 image specs. Each image is either "
                                "LLM-generated SVG (source_type='svg', for "
                                "diagrams/charts/abstract structure) or fetched "
                                "from web image search + VLM verification "
                                "(source_type='web', for photorealistic scenes, "
                                "products, people, brands, textures). Pick "
                                "source_type by SUBJECT — see the SOURCE DECISION "
                                "section in the system prompt."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "slot_id": {
                                        "type": "string",
                                        "description": (
                                            "Stable snake_case id unique within this "
                                            "slide, e.g. 'flow1', 'arch', 'hero'."
                                        ),
                                        "pattern": "^[a-z][a-z0-9_]*$",
                                    },
                                    "aspect_ratio": {
                                        "type": "string",
                                        "enum": list(ALLOWED_ASPECT_RATIOS),
                                    },
                                    "image_type": {
                                        "type": "string",
                                        "enum": list(ALLOWED_IMAGE_TYPES),
                                    },
                                    "source_type": {
                                        "type": "string",
                                        "enum": list(ALLOWED_SOURCE_TYPES),
                                        "description": (
                                            "Where the image comes from, chosen by "
                                            "the SUBJECT of the image. 'web' = "
                                            "fetched from a web image search and "
                                            "verified by a VLM (use for realistic "
                                            "scenes, photos, named products, people, "
                                            "places, brand assets, textures; on "
                                            "verification failure cleanly falls back "
                                            "to svg). 'svg' = LLM-generated inline "
                                            "SVG (use for geometry, flowcharts, "
                                            "diagrams, charts, abstract structure). "
                                            "'user_upload' = pull from the user-"
                                            "uploaded image library listed in the "
                                            "system prompt — REQUIRED to use every "
                                            "library entry before any other source. "
                                            "Default to 'web' when the subject is "
                                            "photorealistic — do NOT default to svg "
                                            "for real-world subjects."
                                        ),
                                    },
                                    "source_ref": {
                                        "type": "string",
                                        "description": (
                                            "Required when source_type is 'web' or "
                                            "'user_upload'. For 'web': a search "
                                            "query (e.g. 'modern coffee shop "
                                            "interior') or an absolute https URL — "
                                            "URL routes through playwright "
                                            "screenshot; query routes through image "
                                            "search. For 'user_upload': the "
                                            "image_id of a library entry. Ignored "
                                            "when source_type='svg'."
                                        ),
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": (
                                            "What the image should depict — concrete "
                                            "enough that an illustrator could draw it "
                                            "without more context. For flowcharts: "
                                            "name the nodes and arrows. For diagrams: "
                                            "name the parts and their relationships. "
                                            "For web images: describe the scene in "
                                            "terms a VLM can verify against the "
                                            "fetched photo."
                                        ),
                                    },
                                },
                                "required": ["slot_id", "aspect_ratio", "image_type", "description"],
                            },
                        },
                    },
                    "required": ["title", "purpose", "key_points"],
                },
            }
        },
        "required": ["slides"],
    },
    groups=["outline_builder"],
)
async def define_outline(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    state = ctx.get("state")
    if state is None:
        return ToolResult.failure("no state in context")

    slides_raw = params.get("slides")
    if not isinstance(slides_raw, list) or not slides_raw:
        return ToolResult.failure("slides must be a non-empty array")

    if len(slides_raw) < MIN_SLIDES:
        return ToolResult.failure(
            f"deck must have at least {MIN_SLIDES} slides (got {len(slides_raw)}); "
            f"pyramid structure needs Overview + content + Synthesis"
        )
    if len(slides_raw) > MAX_SLIDES:
        return ToolResult.failure(
            f"deck must have at most {MAX_SLIDES} slides (got {len(slides_raw)}); "
            f"split the topic or tighten coverage"
        )

    cleaned: List[Dict[str, Any]] = []
    for i, s in enumerate(slides_raw):
        if not isinstance(s, dict):
            return ToolResult.failure(f"slide {i + 1} is not an object")
        title = s.get("title")
        if not isinstance(title, str) or not title.strip():
            return ToolResult.failure(f"slide {i + 1}: title must be a non-empty string")
        purpose = s.get("purpose", "")
        key_points = s.get("key_points", [])
        if not isinstance(key_points, list) or not key_points:
            return ToolResult.failure(
                f"slide {i + 1}: key_points must be a non-empty array"
            )
        # Coerce all key_points to strings.
        key_points = [str(p) for p in key_points]
        layout_hint = s.get("layout_hint", "")
        if not isinstance(layout_hint, str):
            layout_hint = str(layout_hint) if layout_hint is not None else ""

        # Validate images array (0-3 specs per slide).
        images_raw = s.get("images")
        if images_raw is None:
            images_clean: List[Dict[str, Any]] = []
        elif not isinstance(images_raw, list):
            return ToolResult.failure(f"slide {i + 1}: images must be an array")
        else:
            if len(images_raw) > MAX_IMAGES_PER_SLIDE:
                return ToolResult.failure(
                    f"slide {i + 1}: at most {MAX_IMAGES_PER_SLIDE} images "
                    f"(got {len(images_raw)})"
                )
            images_clean = []
            seen_slot_ids: set[str] = set()
            for j, spec in enumerate(images_raw):
                if not isinstance(spec, dict):
                    return ToolResult.failure(
                        f"slide {i + 1}: images[{j}] must be an object"
                    )
                slot_id = spec.get("slot_id")
                if not isinstance(slot_id, str) or not slot_id.strip():
                    return ToolResult.failure(
                        f"slide {i + 1}: images[{j}].slot_id must be a non-empty string"
                    )
                slot_id = slot_id.strip()
                # Enforce snake_case id pattern (must start with a lowercase
                # letter; only [a-z0-9_] after). The JSON schema `pattern`
                # is documentation only — OpenAI's function-calling layer
                # doesn't enforce it, so we validate here.
                if not _SLOT_ID_RE.match(slot_id):
                    return ToolResult.failure(
                        f"slide {i + 1}: images[{j}].slot_id {slot_id!r} must "
                        f"match /^[a-z][a-z0-9_]*$/ (lowercase snake_case)"
                    )
                if slot_id in seen_slot_ids:
                    return ToolResult.failure(
                        f"slide {i + 1}: duplicate slot_id {slot_id!r}"
                    )
                seen_slot_ids.add(slot_id)
                aspect = spec.get("aspect_ratio")
                if aspect not in ALLOWED_ASPECT_RATIOS:
                    return ToolResult.failure(
                        f"slide {i + 1} image {slot_id!r}: aspect_ratio must be "
                        f"one of {ALLOWED_ASPECT_RATIOS} (got {aspect!r})"
                    )
                image_type = spec.get("image_type")
                if image_type not in ALLOWED_IMAGE_TYPES:
                    return ToolResult.failure(
                        f"slide {i + 1} image {slot_id!r}: image_type must be "
                        f"one of {ALLOWED_IMAGE_TYPES} (got {image_type!r})"
                    )
                # source_type defaults to "svg" for backward compatibility —
                # an outline produced before the web path existed produces
                # the same spec shape it always did.
                source_type = spec.get("source_type", "svg")
                if source_type not in ALLOWED_SOURCE_TYPES:
                    return ToolResult.failure(
                        f"slide {i + 1} image {slot_id!r}: source_type must be "
                        f"one of {ALLOWED_SOURCE_TYPES} (got {source_type!r})"
                    )
                source_ref = spec.get("source_ref", "")
                if source_type in ("web", "user_upload"):
                    if not isinstance(source_ref, str) or not source_ref.strip():
                        ref_hint = (
                            "a search query or an absolute https URL"
                            if source_type == "web"
                            else "the image_id from the user-uploaded library"
                        )
                        return ToolResult.failure(
                            f"slide {i + 1} image {slot_id!r}: source_ref is "
                            f"required when source_type={source_type!r} "
                            f"(provide {ref_hint})"
                        )
                    source_ref = source_ref.strip()
                else:
                    # svg path ignores source_ref; normalise to empty so
                    # downstream stages don't carry stale values.
                    source_ref = ""
                description = spec.get("description")
                if not isinstance(description, str) or not description.strip():
                    return ToolResult.failure(
                        f"slide {i + 1} image {slot_id!r}: description must be a "
                        f"non-empty string"
                    )
                spec_out: Dict[str, Any] = {
                    "slot_id": slot_id,
                    "aspect_ratio": aspect,
                    "image_type": image_type,
                    "source_type": source_type,
                    "description": description.strip(),
                }
                if source_ref:
                    spec_out["source_ref"] = source_ref
                images_clean.append(spec_out)

        cleaned.append(
            {
                "title": title.strip(),
                "purpose": str(purpose),
                "key_points": key_points,
                "layout_hint": layout_hint.strip(),
                "images": images_clean,
            }
        )

    state.outline = cleaned
    return ToolResult.success(f"outline defined: {len(cleaned)} slides")


# ---------------------------------------------------------------------------
# Progressive outline (Stage 2a + 2b)
#
# Two tools that together replace the one-shot define_outline call:
#   - define_skeleton       (Stage 2a, 1 LLM call): high-level deck structure
#     (thesis + MECE groups + per-slide title/purpose/image_intent). Writes
#     a lightweight outline into state.outline; downstream Stage 2b fills
#     in key_points + images per slide.
#   - define_slide_detail   (Stage 2b, N LLM calls): enriches ONE slide with
#     key_points + layout_hint + images. Validates image specs against the
#     same schema as define_outline. Idempotent: slides already containing
#     key_points are skipped by the caller.
#
# define_outline is kept as the fallback path when the progressive tools
# fail (see orchestrator.py Stage 2).
# ---------------------------------------------------------------------------

# Image intent enum: the skeleton stage commits each slide to one of these
# high-level image "shapes". The detail stage then either produces a spec
# of that type or emits an empty images list (for "none"). Splitting the
# decision into two stages lets the skeleton stage plan overall image
# distribution across the deck, while the detail stage decides concrete
# source_type / source_ref per slide.
ALLOWED_IMAGE_INTENTS = (
    "none",
    "hero",
    "flowchart",
    "diagram",
    "chart",
    "icon_cluster",
    "illustration",
)

# snake_case group id: same character set as slot_id but prefixed with
# a 'g' by convention (g1, g2, ...). Pattern kept lenient to allow
# descriptive ids like "market_analysis".
_GROUP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_image_spec(spec: Any, slide_idx_label: str, slot_j: int) -> Dict[str, Any]:
    """Validate a single image spec dict against the shared schema.

    Factored out so define_outline and define_slide_detail enforce the
    same rules without copy-paste. Returns the cleaned spec dict on
    success, raises ValueError with a contextual message on failure
    (callers wrap the message into a ToolResult.failure).
    """
    if not isinstance(spec, dict):
        raise ValueError(f"{slide_idx_label}: images[{slot_j}] must be an object")

    slot_id = spec.get("slot_id")
    if not isinstance(slot_id, str) or not slot_id.strip():
        raise ValueError(f"{slide_idx_label}: images[{slot_j}].slot_id must be a non-empty string")
    slot_id = slot_id.strip()
    if not _SLOT_ID_RE.match(slot_id):
        raise ValueError(
            f"{slide_idx_label}: images[{slot_j}].slot_id {slot_id!r} must "
            f"match /^[a-z][a-z0-9_]*$/ (lowercase snake_case)"
        )

    aspect = spec.get("aspect_ratio")
    if aspect not in ALLOWED_ASPECT_RATIOS:
        raise ValueError(
            f"{slide_idx_label} image {slot_id!r}: aspect_ratio must be "
            f"one of {ALLOWED_ASPECT_RATIOS} (got {aspect!r})"
        )

    image_type = spec.get("image_type")
    if image_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError(
            f"{slide_idx_label} image {slot_id!r}: image_type must be "
            f"one of {ALLOWED_IMAGE_TYPES} (got {image_type!r})"
        )

    source_type = spec.get("source_type", "svg")
    if source_type not in ALLOWED_SOURCE_TYPES:
        raise ValueError(
            f"{slide_idx_label} image {slot_id!r}: source_type must be "
            f"one of {ALLOWED_SOURCE_TYPES} (got {source_type!r})"
        )
    source_ref = spec.get("source_ref", "")
    if source_type in ("web", "user_upload"):
        if not isinstance(source_ref, str) or not source_ref.strip():
            ref_hint = (
                "a search query or an absolute https URL"
                if source_type == "web"
                else "the image_id from the user-uploaded library"
            )
            raise ValueError(
                f"{slide_idx_label} image {slot_id!r}: source_ref is "
                f"required when source_type={source_type!r} (provide "
                f"{ref_hint})"
            )
        source_ref = source_ref.strip()
    else:
        source_ref = ""

    description = spec.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"{slide_idx_label} image {slot_id!r}: description must be a non-empty string"
        )

    spec_out: Dict[str, Any] = {
        "slot_id": slot_id,
        "aspect_ratio": aspect,
        "image_type": image_type,
        "source_type": source_type,
        "description": description.strip(),
    }
    if source_ref:
        spec_out["source_ref"] = source_ref
    return spec_out


@tool(
    name="define_skeleton",
    description=(
        "Define the deck SKELETON (Stage 2a of progressive outline): "
        "central thesis, MECE argument groups, and a per-slide skeleton "
        "(title / purpose / group_id / layout_intent / image_intent). "
        "Call this ONCE. A follow-up stage will fill in key_points and "
        "images per slide based on the image_intent you commit here."
    ),
    params={
        "type": "object",
        "properties": {
            "thesis": {
                "type": "string",
                "description": "Central thesis of the deck in one clear sentence.",
            },
            "groups": {
                "type": "array",
                "description": (
                    "2-4 MECE argument groups. Each group carries an id, a "
                    "short name, and the list of slide indices (0-based) "
                    "that belong to it. Every slide index must appear in "
                    "exactly one group."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                        "name": {"type": "string"},
                        "slide_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                    },
                    "required": ["id", "name", "slide_indices"],
                },
            },
            "slides": {
                "type": "array",
                "description": (
                    "Per-slide skeleton. Length must be within the deck "
                    "size bounds. Each slide carries title, purpose, "
                    "group_id (must match one of the groups above), a "
                    "short layout_intent (e.g. 'hero_cover', "
                    "'two_column_compare', 'section_divider'), and an "
                    "image_intent committing this slide to an image "
                    "category (or 'none' for pure-text slides)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "purpose": {"type": "string"},
                        "group_id": {"type": "string"},
                        "layout_intent": {"type": "string"},
                        "image_intent": {
                            "type": "string",
                            "enum": list(ALLOWED_IMAGE_INTENTS),
                        },
                    },
                    "required": ["title", "purpose", "group_id", "layout_intent", "image_intent"],
                },
            },
        },
        "required": ["thesis", "groups", "slides"],
    },
    groups=["skeleton_builder"],
)
async def define_skeleton(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Stage 2a tool: write deck skeleton + initialize lightweight outline.

    On success, ``state.deck_skeleton`` holds the structure and
    ``state.outline`` is initialized with one entry per slide containing
    only skeleton fields (title / purpose / group_id / layout_intent /
    image_intent, plus an empty key_points and images list as
    placeholders). Stage 2b (define_slide_detail) upgrades each entry
    in-place.
    """
    state = ctx.get("state")
    if state is None:
        return ToolResult.failure("no state in context")

    thesis = params.get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        return ToolResult.failure("thesis must be a non-empty string")
    thesis = thesis.strip()

    groups_raw = params.get("groups")
    if not isinstance(groups_raw, list) or not groups_raw:
        return ToolResult.failure("groups must be a non-empty array")

    # Validate groups and build an id -> group map.
    group_ids: set[str] = set()
    covered_indices: set[int] = set()
    groups_clean: List[Dict[str, Any]] = []
    for i, g in enumerate(groups_raw):
        if not isinstance(g, dict):
            return ToolResult.failure(f"groups[{i}] must be an object")
        gid = g.get("id")
        if not isinstance(gid, str) or not _GROUP_ID_RE.match(gid or ""):
            return ToolResult.failure(
                f"groups[{i}].id must match /^[a-z][a-z0-9_]*$/ (got {gid!r})"
            )
        if gid in group_ids:
            return ToolResult.failure(f"groups[{i}].id {gid!r} is duplicate")
        group_ids.add(gid)
        gname = g.get("name")
        if not isinstance(gname, str) or not gname.strip():
            return ToolResult.failure(f"groups[{i}].name must be a non-empty string")
        gidx = g.get("slide_indices")
        if not isinstance(gidx, list) or not gidx:
            return ToolResult.failure(
                f"groups[{i}].slide_indices must be a non-empty array of integers"
            )
        gidx_clean: List[int] = []
        for idx in gidx:
            if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
                return ToolResult.failure(
                    f"groups[{i}].slide_indices must contain non-negative integers (got {idx!r})"
                )
            if idx in covered_indices:
                return ToolResult.failure(
                    f"slide index {idx} appears in more than one group (MECE violation)"
                )
            covered_indices.add(idx)
            gidx_clean.append(idx)
        groups_clean.append({"id": gid, "name": gname.strip(), "slide_indices": gidx_clean})

    slides_raw = params.get("slides")
    if not isinstance(slides_raw, list) or not slides_raw:
        return ToolResult.failure("slides must be a non-empty array")
    if len(slides_raw) < MIN_SLIDES:
        return ToolResult.failure(
            f"deck must have at least {MIN_SLIDES} slides (got {len(slides_raw)}); "
            f"pyramid structure needs Overview + content + Synthesis"
        )
    if len(slides_raw) > MAX_SLIDES:
        return ToolResult.failure(
            f"deck must have at most {MAX_SLIDES} slides (got {len(slides_raw)}); "
            f"split the topic or tighten coverage"
        )

    # Validate each slide's skeleton fields + group membership.
    outline_init: List[Dict[str, Any]] = []
    slide_intents: List[Dict[str, Any]] = []
    for i, s in enumerate(slides_raw):
        if not isinstance(s, dict):
            return ToolResult.failure(f"slide {i + 1} is not an object")
        title = s.get("title")
        if not isinstance(title, str) or not title.strip():
            return ToolResult.failure(f"slide {i + 1}: title must be a non-empty string")
        purpose = s.get("purpose", "")
        if not isinstance(purpose, str) or not purpose.strip():
            return ToolResult.failure(f"slide {i + 1}: purpose must be a non-empty string")
        gid = s.get("group_id")
        if not isinstance(gid, str) or gid not in group_ids:
            return ToolResult.failure(
                f"slide {i + 1}: group_id {gid!r} must match one of the defined groups {sorted(group_ids)}"
            )
        # MECE = Collectively Exhaustive: every slide MUST belong to
        # exactly one group. The prompt's GROUP design section tells the
        # LLM to create a dedicated "overview" group covering slide 0 and
        # slide N-1, but some models miss this on first attempt — the
        # explicit failure message below mirrors the prompt's wording so
        # a retry produces the fix the prompt was asking for.
        if i not in covered_indices:
            return ToolResult.failure(
                f"slide {i + 1} (index {i}) is not covered by any group's "
                f"slide_indices. MECE requires EVERY slide 0..N-1 to appear "
                f"in exactly one group. Fix: add index {i} to the most "
                f"relevant existing group, or create a dedicated 'overview' "
                f"group covering slide 0 and slide N-1 together."
            )
        layout_intent = s.get("layout_intent", "")
        if not isinstance(layout_intent, str) or not layout_intent.strip():
            return ToolResult.failure(f"slide {i + 1}: layout_intent must be a non-empty string")
        image_intent = s.get("image_intent")
        if image_intent not in ALLOWED_IMAGE_INTENTS:
            return ToolResult.failure(
                f"slide {i + 1}: image_intent must be one of {ALLOWED_IMAGE_INTENTS} (got {image_intent!r})"
            )

        outline_init.append(
            {
                "title": title.strip(),
                "purpose": purpose.strip(),
                # key_points / images are filled in by Stage 2b.
                # Initialize to empty so downstream code reading
                # slide.get("images", []) doesn't crash on missing key.
                "key_points": [],
                "layout_hint": layout_intent.strip(),
                "images": [],
                # Internal marker: Stage 2b flips this to True after
                # detail is written. Lets the detail_generator skip
                # already-enriched slides on retry / resume.
                "_detail_filled": False,
            }
        )
        slide_intents.append(
            {"group_id": gid, "layout_intent": layout_intent.strip(), "image_intent": image_intent}
        )

    # Commit. Stage 2b reads state.outline[i] to know the skeleton and
    # writes key_points / images / refined layout_hint back into the
    # same slot.
    state.deck_skeleton = {
        "thesis": thesis,
        "groups": groups_clean,
        "slide_intents": slide_intents,
    }
    state.outline = outline_init
    return ToolResult.success(
        f"skeleton defined: {len(outline_init)} slides, {len(groups_clean)} groups"
    )


@tool(
    name="define_slide_detail",
    description=(
        "Fill in the DETAIL (Stage 2b) for ONE slide: key_points, refined "
        "layout_hint, and image specs. Call this ONCE per slide_index. "
        "The slide_index must match a slot created by define_skeleton. "
        "image specs follow the same schema as define_outline "
        "(slot_id / aspect_ratio / image_type / source_type / source_ref / "
        "description). image_type must be compatible with the image_intent "
        "committed at the skeleton stage."
    ),
    params={
        "type": "object",
        "properties": {
            "slide_index": {
                "type": "integer",
                "description": "0-based index of the slide to enrich. Must be in range.",
            },
            "key_points": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 5,
                "description": "2-5 concrete content-bearing sentences.",
            },
            "layout_hint": {
                "type": "string",
                "description": (
                    "Refined free-text layout description (more concrete "
                    "than the skeleton's layout_intent). Should differ "
                    "from previous slides' layouts to avoid repetition."
                ),
            },
            "images": {
                "type": "array",
                "maxItems": MAX_IMAGES_PER_SLIDE,
                "description": (
                    "0-3 image specs matching the skeleton's image_intent. "
                    "image_type must equal image_intent unless "
                    "image_intent='illustration' (which accepts either "
                    "illustration or hero). Pass [] when image_intent='none'."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "slot_id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                        "aspect_ratio": {"type": "string", "enum": list(ALLOWED_ASPECT_RATIOS)},
                        "image_type": {"type": "string", "enum": list(ALLOWED_IMAGE_TYPES)},
                        "source_type": {"type": "string", "enum": list(ALLOWED_SOURCE_TYPES)},
                        "source_ref": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["slot_id", "aspect_ratio", "image_type", "description"],
                },
            },
        },
        "required": ["slide_index", "key_points", "layout_hint"],
    },
    groups=["slide_detail_builder"],
)
async def define_slide_detail(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Stage 2b tool: enrich one slide with key_points + images.

    Reads ``state.outline[slide_index]`` (populated by define_skeleton),
    validates the incoming detail against the same schema as
    define_outline, and writes the merged slide back into the same slot.
    Idempotent: re-calling with the same slide_index overwrites the
    previous detail (used by the retry loop).
    """
    state = ctx.get("state")
    if state is None:
        return ToolResult.failure("no state in context")

    slide_index = params.get("slide_index")
    if not isinstance(slide_index, int) or isinstance(slide_index, bool):
        return ToolResult.failure(
            f"slide_index must be an integer (got {type(slide_index).__name__})"
        )
    if slide_index < 0 or slide_index >= len(state.outline):
        return ToolResult.failure(
            f"slide_index {slide_index} out of range (deck has {len(state.outline)} slides)"
        )

    key_points = params.get("key_points")
    if not isinstance(key_points, list):
        return ToolResult.failure("key_points must be an array")
    if len(key_points) < 2 or len(key_points) > 5:
        return ToolResult.failure(
            f"key_points must contain 2-5 items (got {len(key_points)})"
        )
    key_points_clean: List[str] = []
    for j, kp in enumerate(key_points):
        if not isinstance(kp, str) or not kp.strip():
            return ToolResult.failure(f"key_points[{j}] must be a non-empty string")
        key_points_clean.append(kp.strip())

    layout_hint = params.get("layout_hint", "")
    if not isinstance(layout_hint, str) or not layout_hint.strip():
        return ToolResult.failure("layout_hint must be a non-empty string")

    # Resolve image_intent from the skeleton (if available) so we can
    # enforce the image_type == image_intent contract. When deck_skeleton
    # is missing (e.g. tests constructing state manually), skip this
    # check and fall back to the same validation as define_outline.
    image_intent = None
    if state.deck_skeleton is not None:
        intents = state.deck_skeleton.get("slide_intents") or []
        if 0 <= slide_index < len(intents):
            image_intent = intents[slide_index].get("image_intent")

    images_raw = params.get("images")
    if images_raw is None:
        images_clean: List[Dict[str, Any]] = []
    elif not isinstance(images_raw, list):
        return ToolResult.failure("images must be an array")
    else:
        if len(images_raw) > MAX_IMAGES_PER_SLIDE:
            return ToolResult.failure(
                f"at most {MAX_IMAGES_PER_SLIDE} images per slide (got {len(images_raw)})"
            )
        images_clean = []
        seen_slot_ids: set[str] = set()
        slide_label = f"slide {slide_index + 1}"
        for j, spec in enumerate(images_raw):
            try:
                spec_out = _validate_image_spec(spec, slide_label, j)
            except ValueError as exc:
                return ToolResult.failure(str(exc))
            if spec_out["slot_id"] in seen_slot_ids:
                return ToolResult.failure(
                    f"{slide_label}: duplicate slot_id {spec_out['slot_id']!r}"
                )
            seen_slot_ids.add(spec_out["slot_id"])
            # Enforce the skeleton-stage commitment: image_type must
            # match image_intent. The only flexibility is that
            # image_intent="illustration" tolerates either "illustration"
            # or "hero" (a hero spot is still a spot illustration).
            if image_intent and image_intent != "none":
                allowed_types = (
                    {"illustration", "hero"} if image_intent == "illustration" else {image_intent}
                )
                if spec_out["image_type"] not in allowed_types:
                    return ToolResult.failure(
                        f"{slide_label} image {spec_out['slot_id']!r}: image_type "
                        f"{spec_out['image_type']!r} must match image_intent "
                        f"{image_intent!r} committed at skeleton stage"
                    )
            elif image_intent == "none":
                return ToolResult.failure(
                    f"{slide_label}: skeleton committed image_intent='none' but "
                    f"image spec {spec_out['slot_id']!r} was provided"
                )
            images_clean.append(spec_out)

    # Anti-correlation check: image_intent != 'none' should produce at
    # least one image. Soft-enforced as a warning rather than a hard
    # failure so the LLM can recover by re-calling with images in retry.
    if (
        image_intent
        and image_intent != "none"
        and not images_clean
    ):
        return ToolResult.failure(
            f"slide {slide_index + 1}: skeleton committed image_intent={image_intent!r} "
            f"but images is empty. Provide at least one {image_intent!r} image spec, "
            f"or re-plan the skeleton with image_intent='none' for this slide."
        )

    # Merge skeleton + detail into the same slot. Skeleton fields
    # (title / purpose) are preserved; detail fields overwrite the
    # placeholders.
    current = state.outline[slide_index]
    current["key_points"] = key_points_clean
    current["layout_hint"] = layout_hint.strip()
    current["images"] = images_clean
    current["_detail_filled"] = True
    return ToolResult.success(
        f"slide {slide_index + 1} detail filled: {len(key_points_clean)} key_points, "
        f"{len(images_clean)} images"
    )
