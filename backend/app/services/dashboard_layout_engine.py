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
        "text_widget",
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
    "page_title": {"size": "full", "height": 2},
    "section_title": {"size": "full", "height": 2},
    "hero": {"size": "full", "height": 10},
    "kpi": {"size": "small", "height": 3},
    "primary_visual": {"size": "full", "height": 8},
    "supporting_visual": {"size": "medium", "height": 6},
    "detail": {"size": "full", "height": 8},
    "context_text": {"size": "medium", "height": 3},
    "insight_callout": {"size": "medium", "height": 3},
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
    if spec.type == "text_widget":
        if spec.role in ("page_title", "section_title"):
            height = 2
        elif spec.role in ("insight_callout",):
            height = 3
        else:
            height = 3
    elif spec.type == "filter_bar":
        height = 2
    elif spec.type in ("card", "container", "section", "column_layout"):
        # Containers: height will be computed from children
        height = 0  # placeholder, computed later
    else:
        # visualization and others
        if size == "full":
            height = 8
        elif size in ("large", "xl"):
            height = 7
        elif size == "small":
            height = 3
        elif size == "xs":
            height = 2
        else:
            height = 5

    # Allow role to override height when provided
    if "height" in role_defaults and spec.type not in ("card", "container", "section", "column_layout"):
        height = role_defaults["height"]

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
    row_gap: int = 1,
) -> tuple[List[Dict[str, Any]], int]:
    """
    Lay out a flat list of blocks in rows across the 12-column grid.
    Returns (layout_blocks, next_y).
    """
    if not blocks:
        return [], start_y

    importance_rank = {"primary": 0, "secondary": 1, "tertiary": 2}
    role_rank = {
        "page_title": 0,
        "section_title": 1,
        "filter_bar": 2,
        "hero": 3,
        "kpi": 4,
        "primary_visual": 5,
        "supporting_visual": 6,
        "detail": 7,
        "context_text": 8,
        "insight_callout": 9,
    }

    # Sort by importance, role, then order
    sorted_blocks = sorted(
        blocks,
        key=lambda b: (
            importance_rank.get(b.importance, 1),
            role_rank.get(b.role, 99),
            b.order,
        ),
    )

    layout_blocks: List[Dict[str, Any]] = []
    row_y = start_y
    row_x = 0
    row_max_h = 0

    for b in sorted_blocks:
        block = _layout_single_block(b, row_x, row_y)
        w = block["width"]
        h = block["height"]

        # Wrap to next row if no space
        if row_x + w > GRID_COLS and row_x != 0:
            row_y += row_max_h + row_gap
            row_x = 0
            row_max_h = 0
            block["x"] = row_x
            block["y"] = row_y

        layout_blocks.append(block)
        row_x += w
        row_max_h = max(row_max_h, h)

    next_y = row_y + row_max_h + row_gap
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

    elif spec.type == "text_widget":
        block["content"] = spec.content or ""
        if spec.variant:
            block["variant"] = spec.variant

    elif spec.type in ("card", "container", "section"):
        # Recursively layout children
        if spec.children:
            child_blocks, child_height = _layout_flat_blocks(spec.children, start_y=0, row_gap=1)
            block["children"] = child_blocks
            # Container height = children height + padding
            padding = 2 if spec.type == "card" else 1
            block["height"] = child_height + padding
        else:
            block["children"] = []
            block["height"] = 3  # empty container min height

        # Chrome/title for cards/containers
        if spec.chrome:
            block["chrome"] = spec.chrome.model_dump(exclude_none=True)
        elif spec.type in ("card", "section"):
            # Default chrome from title if provided
            if spec.view_overrides and spec.view_overrides.get("title"):
                block["chrome"] = {"title": spec.view_overrides.get("title"), "showHeader": True}

    elif spec.type == "column_layout":
        # Layout each column's children relative to column start
        if spec.columns:
            col_x = 0
            max_col_height = 0
            rendered_columns: List[Dict[str, Any]] = []

            for col in spec.columns:
                col_span = min(col.span, GRID_COLS - col_x)
                if col.children:
                    # Layout children within column (relative y=0)
                    col_children, col_height = _layout_flat_blocks(col.children, start_y=0, row_gap=1)
                    # Scale children widths to fit within column span
                    for child in col_children:
                        # Children x is relative to column
                        child_w = child.get("width", 6)
                        # Scale proportionally if needed
                        if child_w > col_span:
                            child["width"] = col_span
                else:
                    col_children = []
                    col_height = 2

                rendered_columns.append({
                    "span": col_span,
                    "children": col_children,
                })
                max_col_height = max(max_col_height, col_height)
                col_x += col_span

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
    
    The LLM outputs semantic hints (role, size, importance) and this engine
    computes the actual grid positions.
    """
    if not semantic_blocks:
        return {"blocks": []}

    # Group by section
    by_section: Dict[str, List[DashboardBlockSpec]] = {}
    for b in semantic_blocks:
        sec = b.section or "main"
        by_section.setdefault(sec, []).append(b)

    # Stable section order: "main" / "overview" first, then alphabetical
    def _section_sort_key(name: str) -> tuple[int, str]:
        if name in ("main", "overview"):
            return (0, name)
        return (1, name)

    section_names = sorted(by_section.keys(), key=_section_sort_key)

    all_blocks: List[Dict[str, Any]] = []
    current_y = 0
    row_gap = 1

    for section in section_names:
        section_blocks = by_section[section]
        laid_out, next_y = _layout_flat_blocks(section_blocks, start_y=current_y, row_gap=row_gap)
        all_blocks.extend(laid_out)
        current_y = next_y

    return {"blocks": all_blocks}



