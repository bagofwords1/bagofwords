from typing import List, Dict, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


GRID_COLS = 12


# -----------------------------------------------------------------------------
# Filter and Chrome definitions
# -----------------------------------------------------------------------------

class FilterControl(BaseModel):
    """Control type and options for a filter."""
    kind: Literal["select", "multi", "daterange", "search", "checkbox", "radio"] = "select"
    label: Optional[str] = None
    options: Optional[List[Dict[str, Any]]] = None  # [{label, value}]
    default: Optional[Any] = None
    placeholder: Optional[str] = None


class FilterBinding(BaseModel):
    """How a filter connects to widgets."""
    scope: Literal["report", "container", "explicit"] = "report"
    targets: Optional[List[str]] = None  # widget_ids if explicit
    tags: Optional[List[str]] = None     # alternative grouping selector
    param_key: str                       # parameter name consumed by widgets


class FilterSpec(BaseModel):
    """A filter definition for filter_bar blocks."""
    control: FilterControl
    binding: FilterBinding


class ContainerChrome(BaseModel):
    """Visual chrome for cards/containers."""
    title: Optional[str] = None
    subtitle: Optional[str] = None
    showHeader: bool = True
    border: Literal["none", "soft", "strong"] = "soft"
    padding: int = 2
    background: Optional[str] = None


class ColumnSpec(BaseModel):
    """A column within a column_layout block."""
    span: int = 6  # out of 12
    children: List["DashboardBlockSpec"] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Main block spec
# -----------------------------------------------------------------------------

class DashboardBlockSpec(BaseModel):
    """
    Semantic description of a dashboard block, without concrete x/y/width/height.

    This is intended to be produced by the create_dashboard tool and then
    converted into concrete layout blocks by the layout engine.
    """

    type: Literal[
        "visualization",
        "text",  # inline text (AI-generated)
        "text_widget",  # legacy text widget with DB reference
        "card",
        "container",
        "section",
        "column_layout",
        "filter_bar",
    ]
    
    # For visualization blocks
    visualization_id: Optional[str] = None
    
    # For text_widget blocks
    content: Optional[str] = None
    variant: Optional[Literal["title", "subtitle", "paragraph", "insight", "summary"]] = None
    
    # For card/container/section blocks (nesting)
    children: Optional[List["DashboardBlockSpec"]] = None
    chrome: Optional[ContainerChrome] = None
    
    # For column_layout blocks
    columns: Optional[List[ColumnSpec]] = None
    
    # For filter_bar blocks
    filters: Optional[List[FilterSpec]] = None
    sticky: bool = False

    # Semantic layout hints
    role: Literal[
        "page_title",
        "section_title",
        "hero",
        "kpi",
        "primary_visual",
        "supporting_visual",
        "detail",
        "context_text",
        "insight_callout",
        "filter_bar",
    ] = "supporting_visual"
    importance: Literal["primary", "secondary", "tertiary"] = "secondary"
    size: Literal["xs", "small", "medium", "large", "xl", "full"] = "medium"
    section: Optional[str] = None
    group_id: Optional[str] = None
    order: int = 0

    # Styling – passed through to layout block as view_overrides
    view_overrides: Optional[Dict[str, Any]] = None


# Enable forward references for nested children
ColumnSpec.model_rebuild()
DashboardBlockSpec.model_rebuild()


# -----------------------------------------------------------------------------
# Size/role defaults
# -----------------------------------------------------------------------------

ROLE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "page_title": {"size": "full", "height": 3},      # Taller for main titles
    "section_title": {"size": "full", "height": 3},   # Taller for section headers
    "hero": {"size": "full", "height": 10},
    "kpi": {"size": "small", "height": 4},            # Slightly taller for sparklines
    "primary_visual": {"size": "large", "height": 9},
    "supporting_visual": {"size": "medium", "height": 7},
    "detail": {"size": "medium", "height": 6},
    "context_text": {"size": "full", "height": 3},    # Taller for readability
    "insight_callout": {"size": "medium", "height": 4},
    "filter_bar": {"size": "full", "height": 2},
}

SIZE_TO_WIDTH: Dict[str, int] = {
    "xs": 2,
    "small": 3,
    "medium": 6,
    "large": 8,
    "xl": 10,
    "full": 12,
}

# Height defaults by size (used when role doesn't specify)
SIZE_TO_HEIGHT: Dict[str, int] = {
    "xs": 3,
    "small": 4,
    "medium": 7,
    "large": 9,
    "xl": 10,
    "full": 9,
}


def _resolve_size(spec: DashboardBlockSpec) -> tuple[int, int]:
    """
    Map (role, size, type) → (width, height) in grid units.
    """
    # Base defaults by role
    role_defaults = ROLE_DEFAULTS.get(spec.role, {})
    base_size = role_defaults.get("size", spec.size or "medium")
    size = spec.size or base_size

    width = SIZE_TO_WIDTH.get(size, 6)

    # Height heuristics by type
    if spec.type in ("text", "text_widget"):
        if spec.role in ("page_title",):
            height = 3  # Main page title needs more space
        elif spec.role in ("section_title",):
            height = 3  # Section headers need visibility
        elif spec.role in ("insight_callout",):
            height = 4
        elif spec.variant == "title":
            height = 3  # Titles need breathing room
        elif spec.variant == "subtitle":
            height = 2  # Subtitles slightly taller
        else:
            height = 2  # Default text height
    elif spec.type == "filter_bar":
        height = 2
    elif spec.type in ("card", "container", "section", "column_layout"):
        # Containers: height will be computed from children
        height = 0  # placeholder, computed later
    else:
        # visualization and others - use role height if available, else size-based
        if "height" in role_defaults:
            height = role_defaults["height"]
        else:
            height = SIZE_TO_HEIGHT.get(size, 7)

    # Clamp width to grid columns
    width = max(1, min(width, GRID_COLS))
    height = max(1, height) if height > 0 else 0

    return width, height


def _compute_children_bounds(children_blocks: List[Dict[str, Any]]) -> tuple[int, int]:
    """
    Compute the bounding box (width, height) of a list of positioned child blocks.
    Returns (max_width, total_height).
    """
    if not children_blocks:
        return (GRID_COLS, 2)  # default empty container size
    
    max_x_end = 0
    max_y_end = 0
    for b in children_blocks:
        x_end = b.get("x", 0) + b.get("width", 0)
        y_end = b.get("y", 0) + b.get("height", 0)
        max_x_end = max(max_x_end, x_end)
        max_y_end = max(max_y_end, y_end)
    
    return (min(max_x_end, GRID_COLS), max_y_end)


def _layout_flat_blocks(
    blocks: List[DashboardBlockSpec],
    start_y: int = 0,
    row_gap: int = 0,
) -> tuple[List[Dict[str, Any]], int]:
    """
    Lay out a flat list of blocks in rows across the 12-column grid.
    Returns (layout_blocks, next_y).
    
    Blocks are laid out in their INPUT ORDER - the LLM's output order
    represents the intended narrative flow and should be preserved.
    
    Note: row_gap is only added BETWEEN rows, not after the last row.
    """
    if not blocks:
        return [], start_y

    layout_blocks: List[Dict[str, Any]] = []
    row_y = start_y
    row_x = 0
    row_max_h = 0

    # Preserve input order - the LLM outputs blocks in the intended sequence
    for b in blocks:
        block = _layout_single_block(b, row_x, row_y)
        w = block["width"]
        h = block["height"]

        # Wrap to next row if no space
        if row_x + w > GRID_COLS and row_x != 0:
            row_y += row_max_h + row_gap  # gap only BETWEEN rows
            row_x = 0
            row_max_h = 0
            block["x"] = row_x
            block["y"] = row_y

        layout_blocks.append(block)
        row_x += w
        row_max_h = max(row_max_h, h)

    # No trailing gap - next_y is exactly where the next block should start
    next_y = row_y + row_max_h
    return layout_blocks, next_y


def _layout_single_block(spec: DashboardBlockSpec, x: int, y: int) -> Dict[str, Any]:
    """
    Convert a single DashboardBlockSpec to a layout block dict.
    Handles nesting for containers, cards, sections, and column_layout.
    """
    w, h = _resolve_size(spec)

    block: Dict[str, Any] = {
        "type": spec.type,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
    }

    # Pass through view_overrides
    if spec.view_overrides is not None:
        block["view_overrides"] = spec.view_overrides

    # Type-specific handling
    if spec.type == "visualization":
        block["visualization_id"] = spec.visualization_id

    elif spec.type in ("text", "text_widget"):
        block["content"] = spec.content or ""
        if spec.variant:
            block["variant"] = spec.variant

    elif spec.type in ("card", "container", "section"):
        # Chrome/title for cards - add header height to calculations
        header_height = 0
        if spec.chrome:
            block["chrome"] = spec.chrome.model_dump(exclude_none=True)
            if spec.chrome.showHeader and (spec.chrome.title or spec.chrome.subtitle):
                header_height = 2  # Space for header
        
        # Recursively layout children
        if spec.children:
            child_blocks, child_height = _layout_flat_blocks(spec.children, start_y=0, row_gap=0)
            block["children"] = child_blocks
            # Container height = header + children height (no extra padding)
            block["height"] = header_height + child_height
        else:
            block["children"] = []
            block["height"] = header_height + 3  # empty container min height

    elif spec.type == "column_layout":
        # Layout each column's children relative to column start
        if spec.columns:
            max_col_height = 0
            rendered_columns: List[Dict[str, Any]] = []

            for col in spec.columns:
                col_span = col.span
                if col.children:
                    # Layout children within column - children use full width within their column
                    col_children = []
                    col_y = 0
                    for child_spec in col.children:
                        child_block = _layout_single_block(child_spec, 0, col_y)
                        # Children within a column should fill the column width
                        child_block["width"] = col_span
                        col_children.append(child_block)
                        col_y += child_block["height"]
                    col_height = col_y
                else:
                    col_children = []
                    col_height = 2

                rendered_columns.append({
                    "span": col_span,
                    "children": col_children,
                })
                max_col_height = max(max_col_height, col_height)

            block["columns"] = rendered_columns
            block["height"] = max_col_height
            block["width"] = GRID_COLS  # column_layout always full width
        else:
            block["columns"] = []
            block["height"] = 2

    elif spec.type == "filter_bar":
        block["filters"] = [f.model_dump() for f in (spec.filters or [])]
        block["sticky"] = spec.sticky
        block["height"] = 2
        block["width"] = GRID_COLS  # filter bar always full width

    return block


def compute_layout(semantic_blocks: List[DashboardBlockSpec]) -> Dict[str, Any]:
    """
    Turn semantic block specs into concrete layout blocks with x, y, width, height.

    Handles:
    - Section grouping (blocks with same section are laid out together)
    - Nested containers (card, container, section with children)
    - Column layouts (horizontal splits)
    - Filter bars (sticky filter rows)
    
    Blocks are laid out preserving the LLM's output order - the sequence
    represents the intended narrative flow.
    """
    if not semantic_blocks:
        return {"blocks": []}

    # Group by section, preserving first-appearance order of sections
    by_section: Dict[str, List[DashboardBlockSpec]] = {}
    section_order: List[str] = []  # Track order sections first appear
    for b in semantic_blocks:
        sec = b.section or "main"
        if sec not in by_section:
            section_order.append(sec)
            by_section[sec] = []
        by_section[sec].append(b)

    all_blocks: List[Dict[str, Any]] = []
    current_y = 0

    # Process sections in the order they first appeared (preserves LLM intent)
    # No extra gaps between sections - blocks flow tightly
    for section in section_order:
        section_blocks = by_section[section]
        laid_out, next_y = _layout_flat_blocks(section_blocks, start_y=current_y, row_gap=0)
        all_blocks.extend(laid_out)
        current_y = next_y

    return {"blocks": all_blocks}



