"""
Spatial containment tree for rule-based element classification.

Builds a parent-child tree based on rectangular overlap ratios,
enabling detection of which elements are visually inside others
(e.g. text inside a card, numbered circles inside step containers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class ContainmentTree:
    """Spatial containment relationships between elements.

    Provides O(1) lookup for parent/children of any element by index.
    """

    # parent_of[child_idx] -> parent_idx (or None)
    parent_of: Dict[int, Optional[int]] = field(default_factory=dict)
    # children_of[parent_idx] -> sorted list of child indices
    children_of: Dict[int, List[int]] = field(default_factory=dict)
    # Set of indices that are children of some other element
    child_set: Set[int] = field(default_factory=set)
    # Total number of elements
    n_elements: int = 0

    def is_child(self, idx: int) -> bool:
        return idx in self.child_set

    def get_parent(self, idx: int) -> Optional[int]:
        return self.parent_of.get(idx)

    def get_children(self, idx: int) -> List[int]:
        return self.children_of.get(idx, [])

    def is_container(self, idx: int) -> bool:
        return idx in self.children_of and len(self.children_of[idx]) > 0


def _rect_area(r: dict) -> float:
    return r.get("w", 0) * r.get("h", 0)


def _overlap_area(a: dict, b: dict) -> float:
    """Compute overlapping area between two rects {x, y, w, h} in pct."""
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]

    ox = max(0, min(ax2, bx2) - max(ax1, bx1))
    oy = max(0, min(ay2, by2) - max(ay1, by1))
    return ox * oy


def build_containment_tree(elements: List[dict]) -> ContainmentTree:
    """Build a containment tree from Playwright-extracted elements.

    For each pair (i, j), if element i's bbox fully encloses element j's
    bbox (DOM-box semantics, with sub-pixel tolerance), then i is a
    candidate parent of j. Each element gets at most one parent: the
    smallest container that qualifies.

    The strict 100% containment matches the DOM model where a parent's
    bbox normally encloses every child's bbox. The previous "70% overlap
    + 1.5x area coefficient" heuristic rejected cards whose only child
    nearly fills the card (small padding → area ratio < 1.5x → no
    parent link → _check_card sees no children → card not classified).
    """
    n = len(elements)
    tree = ContainmentTree(n_elements=n)

    if n == 0:
        return tree

    # Precompute rects and areas
    rects = [e.get("rect_pct", {}) for e in elements]
    areas = [_rect_area(r) for r in rects]

    # Tolerance for sub-pixel rounding in extract_layout.js (which rounds
    # all pct values to 0.01). 0.5 pct comfortably covers any rounding
    # noise without admitting genuinely overlapping siblings.
    TOL = 0.5  # pct

    for j in range(n):
        best_parent = None
        best_parent_area = float("inf")

        if areas[j] == 0:
            tree.parent_of[j] = None
            continue

        jx1, jy1 = rects[j].get("x", 0), rects[j].get("y", 0)
        jx2 = jx1 + rects[j].get("w", 0)
        jy2 = jy1 + rects[j].get("h", 0)

        for i in range(n):
            if i == j:
                continue

            # Parent must be strictly larger (rejects identical bboxes,
            # e.g. z-index stacked layers).
            if areas[i] <= areas[j]:
                continue

            ix1, iy1 = rects[i].get("x", 0), rects[i].get("y", 0)
            ix2 = ix1 + rects[i].get("w", 0)
            iy2 = iy1 + rects[i].get("h", 0)

            # Child's bbox must be fully inside parent's bbox (with tol).
            if (jx1 >= ix1 - TOL and jy1 >= iy1 - TOL
                    and jx2 <= ix2 + TOL and jy2 <= iy2 + TOL
                    and areas[i] < best_parent_area):
                best_parent = i
                best_parent_area = areas[i]

        tree.parent_of[j] = best_parent
        if best_parent is not None:
            tree.child_set.add(j)
            if best_parent not in tree.children_of:
                tree.children_of[best_parent] = []
            tree.children_of[best_parent].append(j)

    # Sort children by Y position for consistent ordering
    for parent_idx in tree.children_of:
        tree.children_of[parent_idx].sort(
            key=lambda c: (rects[c].get("y", 0), rects[c].get("x", 0))
        )

    return tree


# ---------------------------------------------------------------------------
# Multi-element pattern detection
# ---------------------------------------------------------------------------

@dataclass
class NumberedGroup:
    """A group of elements forming a numbered step sequence."""
    number_elements: List[int] = field(default_factory=list)  # indices of number circles
    container_elements: List[int] = field(default_factory=list)  # indices of step containers
    numbers: List[int] = field(default_factory=list)  # the actual step numbers


@dataclass
class BulletGroup:
    """A group of elements forming a bullet list."""
    item_elements: List[int] = field(default_factory=list)  # indices of LI/bullet items
    parent_idx: Optional[int] = None  # parent container (if any)


def detect_numbered_sequences(
    elements: List[dict], tree: ContainmentTree
) -> List[NumberedGroup]:
    """Detect numbered step sequences (1, 2, 3...) in the element list.

    Strategy:
    1. Find elements whose directText matches a number pattern
    2. Group them by spatial column (x center within 5%)
    3. Sort by Y and verify sequential numbering
    """
    _NUM_RE = re.compile(r"^([1-9]\d*)[\.\)]?\s*$")

    # Find candidate number elements
    candidates: List[Tuple[int, int, dict]] = []  # (index, number_value, rect)
    for i, elem in enumerate(elements):
        text = elem.get("directText", "").strip() or elem.get("text", "").strip()[:10]
        m = _NUM_RE.match(text)
        if m:
            num = int(m.group(1))
            rect = elem.get("rect_pct", {})
            # Number circles are typically small and square-ish
            w = rect.get("w", 0)
            h = rect.get("h", 0)
            if w < 10 and h < 12:
                candidates.append((i, num, rect))

    if len(candidates) < 2:
        return []

    # Drop candidates that are descendants of another candidate. Without this
    # filter, BOTH the outer circle (`<div class="rounded-full">` whose
    # directText is empty but whose `text` includes the inner span's digit)
    # AND the inner span itself get classified, producing two overlapping
    # numbered_step shapes per card with one of them mis-sized. We keep the
    # outer container because its rect matches the visible circle.
    candidate_idx_set = {c[0] for c in candidates}
    filtered: List[Tuple[int, int, dict]] = []
    for idx, num, rect in candidates:
        ancestor = tree.get_parent(idx)
        nested = False
        while ancestor is not None:
            if ancestor in candidate_idx_set:
                nested = True
                break
            ancestor = tree.get_parent(ancestor)
        if not nested:
            filtered.append((idx, num, rect))
    candidates = filtered
    if len(candidates) < 2:
        return []

    # Group by spatial column (x center within 5%)
    groups: List[List[Tuple[int, int, dict]]] = []
    used = set()

    for ci, (idx, num, rect) in enumerate(candidates):
        if ci in used:
            continue
        group = [(idx, num, rect)]
        used.add(ci)
        cx = rect.get("x", 0) + rect.get("w", 0) / 2

        for cj, (idx2, num2, rect2) in enumerate(candidates):
            if cj in used:
                continue
            cx2 = rect2.get("x", 0) + rect2.get("w", 0) / 2
            if abs(cx - cx2) < 5:
                group.append((idx2, num2, rect2))
                used.add(cj)

        groups.append(group)

    result = []
    for group in groups:
        # Sort by Y position
        group.sort(key=lambda t: t[2].get("y", 0))
        numbers = [t[1] for t in group]
        indices = [t[0] for t in group]

        # Verify sequential numbering (allow gaps)
        if sorted(numbers) == numbers and numbers[0] == 1:
            # Find container elements for each number
            containers = []
            for idx in indices:
                parent = tree.get_parent(idx)
                if parent is not None:
                    containers.append(parent)
                else:
                    containers.append(idx)

            result.append(NumberedGroup(
                number_elements=indices,
                container_elements=containers,
                numbers=numbers,
            ))

    return result


def detect_bullet_groups(
    elements: List[dict], tree: ContainmentTree
) -> List[BulletGroup]:
    """Detect bullet list groups (LI items or elements with list/bullet classes).

    Strategy:
    1. Find elements with tag=LI or class containing list/bullet
    2. Group siblings (same parent in containment tree)
    """
    li_indices: List[int] = []
    class_indices: List[int] = []

    for i, elem in enumerate(elements):
        tag = elem.get("tag", "").upper()
        classes = [c.lower() for c in elem.get("classes", [])]

        if tag == "LI":
            li_indices.append(i)
        elif any(c in ("list", "bullet", "check") for c in classes):
            class_indices.append(i)

    # Group LI elements by parent
    all_li = li_indices + class_indices
    if not all_li:
        return []

    # Group by parent
    parent_groups: Dict[Optional[int], List[int]] = {}
    for idx in all_li:
        parent = tree.get_parent(idx)
        if parent not in parent_groups:
            parent_groups[parent] = []
        parent_groups[parent].append(idx)

    result = []
    for parent_idx, item_indices in parent_groups.items():
        if len(item_indices) >= 2:
            # Sort by Y position
            rects = [elements[i].get("rect_pct", {}) for i in item_indices]
            sorted_pairs = sorted(zip(item_indices, rects), key=lambda p: p[1].get("y", 0))
            sorted_indices = [p[0] for p in sorted_pairs]

            result.append(BulletGroup(
                item_elements=sorted_indices,
                parent_idx=parent_idx,
            ))

    return result


# ---------------------------------------------------------------------------
# Table detection
# ---------------------------------------------------------------------------

@dataclass
class TableGroup:
    """A set of leaf text elements aligned into an R×C grid (a table).

    Works for both real ``<table>`` markup and div+flex+span "div-tables".
    """
    container_idx: int                                   # element whose bbox frames the table
    grid: List[List[int]] = field(default_factory=list)  # grid[row][col] -> element index
    row_container_idx: List[int] = field(default_factory=list)  # per-row row-div index (bg/separator)
    consumed: Set[int] = field(default_factory=set)      # all cell indices (absorbed by the table)


def _all_descendants(tree: "ContainmentTree", idx: int) -> List[int]:
    """Recursively collect all descendant indices of ``idx``."""
    out: List[int] = []
    stack = list(tree.get_children(idx))
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(tree.get_children(c))
    return out


def _is_badge_like(elem: dict) -> bool:
    """Filter badge/pill elements out of table-cell candidates."""
    classes = [c.lower() for c in elem.get("classes", [])]
    if "badge" in classes:
        return True
    rect = elem.get("rect_pct", {})
    styles = elem.get("styles", {})
    w = rect.get("w", 0)
    h = rect.get("h", 0)
    text = elem.get("directText", "").strip() or elem.get("text", "").strip()
    bg = styles.get("backgroundColor")
    has_bg = bg is not None and bg not in ("transparent", "rgba(0, 0, 0, 0)")
    br = styles.get("borderRadius", "")
    try:
        has_radius = float(str(br).replace("px", "")) > 0
    except (ValueError, TypeError):
        has_radius = bool(br)
    return w < 25 and h < 18 and bool(text) and has_bg and has_radius


def _y_overlap_ratio(a: dict, b: dict) -> float:
    """Fraction of the shorter cell's height that the two Y-ranges overlap."""
    ay1, ay2 = a.get("y", 0), a.get("y", 0) + a.get("h", 0)
    by1, by2 = b.get("y", 0), b.get("y", 0) + b.get("h", 0)
    oy = max(0.0, min(ay2, by2) - max(ay1, by1))
    min_h = min(a.get("h", 0), b.get("h", 0))
    if min_h <= 0:
        return 0.0
    return oy / min_h


def detect_tables(elements: List[dict], tree: ContainmentTree) -> List[TableGroup]:
    """Detect table-like grids of leaf text cells.

    A table is a set of >=4 leaf text elements, sharing a common container,
    that align into a regular R x C grid (R>=2, C>=2). Containers are tried
    smallest-area-first so a tight table-frame container wins over a loose
    outer ancestor (whose other descendants would break grid alignment).

    Table-row wrappers (``<thead>``, ``<tbody>``, ``<tfoot>``, ``<tr>``)
    are excluded from being treated as table containers because they only
    contain one section of the table (header OR body), causing the header
    row to be split off from the body. The real container is the parent
    ``<table>`` (or the outer div in a div-table) — which contains all
    rows and whose descendants walk through the wrappers via
    ``_all_descendants``.
    """
    n = len(elements)
    if n == 0:
        return []

    rects = [e.get("rect_pct", {}) for e in elements]
    consumed: Set[int] = set()
    groups: List[TableGroup] = []

    # Only real <table> elements can be table containers. The previous
    # geometric heuristic (any container whose descendants align into a
    # grid) misclassified side-by-side card layouts as tables — when two
    # cards happen to mirror each other's internal structure (title row +
    # paragraph + sub-box, or two parallel bullet lists), their descendants
    # form a perfectly aligned grid drawn from DIFFERENT semantic containers.
    # HTML already disambiguates: a real table uses <table>, parallel cards
    # use div/flex/grid. Trust the markup.
    _TABLE_WRAPPER_TAGS = frozenset({"THEAD", "TBODY", "TFOOT", "TR", "COLGROUP", "COL"})

    container_idxs = [
        i for i in range(n)
        if tree.is_container(i)
        and elements[i].get("tag", "").upper() == "TABLE"
    ]
    # Smallest-area first: the tightest container that frames the grid wins,
    # preventing a loose ancestor (e.g. a content wrapper that also holds a
    # heading) from being mis-picked as the table container.
    container_idxs.sort(key=lambda i: _rect_area(rects[i]))

    for c_idx in container_idxs:
        candidates: List[int] = []
        for d in _all_descendants(tree, c_idx):
            if d in consumed:
                continue
            el = elements[d]
            if el.get("is_icon"):
                continue
            # Skip table-row wrappers even when the containment tree treats
            # them as leaves. With border-collapse rounding, a <tr>'s bbox
            # may not fully enclose its <th>/<td> cells, so the tree can
            # assign the cells' parent to <thead>/<tbody> instead — leaving
            # <tr> as a "leaf" whose text is the concatenation of all its
            # cells. Including it in candidates would inflate one row's
            # column count and break grid alignment.
            if el.get("tag", "").upper() in _TABLE_WRAPPER_TAGS:
                continue
            # Skip (icon + label) pair cells — they belong to an icon_text
            # element, not a table. Pattern:
            #   <div class="flex"><i>icon</i><span>label</span></div>
            # Trigger only when the parent has ≤2 children (the icon + this
            # label), so legitimate table rows that happen to include an
            # icon sibling are not dropped.
            parent_idx = tree.get_parent(d)
            if parent_idx is not None:
                siblings = tree.get_children(parent_idx)
                if len(siblings) <= 2:
                    has_sibling_icon = any(
                        elements[s].get("is_icon")
                        for s in siblings if s != d
                    )
                    if has_sibling_icon:
                        continue
            text = el.get("directText", "").strip() or el.get("text", "").strip()
            if not text:
                continue
            if _is_badge_like(el):
                continue
            # Only leaf cells form the grid — skip row/column containers.
            if tree.is_container(d):
                continue
            candidates.append(d)
        if len(candidates) < 4:
            continue

        # Cluster into rows by Y overlap.
        cand_sorted = sorted(
            candidates,
            key=lambda i: (rects[i].get("y", 0), rects[i].get("x", 0)),
        )
        rows: List[List[int]] = []
        for idx in cand_sorted:
            placed = False
            for row in rows:
                if _y_overlap_ratio(rects[idx], rects[row[0]]) > 0.5:
                    row.append(idx)
                    placed = True
                    break
            if not placed:
                rows.append([idx])

        rows = [r for r in rows if len(r) >= 2]
        if len(rows) < 2:
            continue

        for row in rows:
            row.sort(key=lambda i: rects[i].get("x", 0))

        # Uniform column count + column X-alignment across all rows.
        col_count = len(rows[0])
        if col_count < 2:
            continue
        if any(len(r) != col_count for r in rows):
            continue
        x_tol = 2.0  # pct of slide width
        aligned = True
        for j in range(col_count):
            xs = [rects[r[j]].get("x", 0) for r in rows]
            if max(xs) - min(xs) > x_tol:
                aligned = False
                break
        if not aligned:
            continue

        cells: Set[int] = set()
        for r in rows:
            cells.update(r)
        if cells & consumed:
            continue

        consumed |= cells
        groups.append(TableGroup(
            container_idx=c_idx,
            grid=rows,
            row_container_idx=[tree.get_parent(rows[r][0]) for r in range(len(rows))],
            consumed=cells,
        ))

    return groups
