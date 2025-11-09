from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


# =====================
# Matchers by value type
# =====================

class TextContains(BaseModel):
    type: Literal["text.contains"] = "text.contains"
    value: str


class TextNotContains(BaseModel):
    type: Literal["text.not_contains"] = "text.not_contains"
    value: str


class TextEquals(BaseModel):
    type: Literal["text.equals"] = "text.equals"
    value: str


class TextRegex(BaseModel):
    type: Literal["text.regex"] = "text.regex"
    pattern: str


class NumberCmp(BaseModel):
    type: Literal["number.cmp"] = "number.cmp"
    op: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    value: float


class ListContainsAny(BaseModel):
    type: Literal["list.contains_any"] = "list.contains_any"
    values: List[Any]


class ListContainsAll(BaseModel):
    type: Literal["list.contains_all"] = "list.contains_all"
    values: List[Any]


class LengthCmp(BaseModel):
    type: Literal["length.cmp"] = "length.cmp"
    op: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    value: int


Matcher = Union[
    TextContains,
    TextNotContains,
    TextEquals,
    TextRegex,
    NumberCmp,
    ListContainsAny,
    ListContainsAll,
    LengthCmp,
]


# =====================
# Rule primitives (category/field without paths)
# =====================

class TargetRef(BaseModel):
    """Reference a testable field via catalog category and field keys.

    category: e.g., "tool:create_widget", "metadata", "completion"
    field: key from the category's field list (e.g., "columns", "rows_count", "code", "text")
    occurrence: optional 1-based index to pin a specific tool call (tools only)
    bind: optional label to reuse this specific occurrence in ordering
    """

    category: str
    field: str
    occurrence: Optional[int] = None
    bind: Optional[str] = None


class FieldRule(BaseModel):
    type: Literal["field"] = "field"
    target: TargetRef
    matcher: Matcher


class ToolCallsRule(BaseModel):
    type: Literal["tool.calls"] = "tool.calls"
    tool: str
    min_calls: int = 0
    max_calls: Optional[int] = None


class OrderingStep(BaseModel):
    """Step in the expected tool order.

    You can specify a tool name or a previously bound label via "bind" on ToolFieldRef.
    When a bind is used, the engine will ensure that the same occurrence is referenced.
    """

    tool_or_bind: str = Field(description="Tool name (e.g., create_widget) or bind label")


class OrderingRule(BaseModel):
    type: Literal["ordering"] = "ordering"
    mode: Literal["flexible", "strict", "exact"] = "flexible"
    allow_extra: bool = True
    sequence: List[OrderingStep]


Rule = Union[
    FieldRule,
    ToolCallsRule,
    OrderingRule,
]


class ExpectationsSpec(BaseModel):
    """Root expectations spec for a test case.

    - rules: list of assertions and constraints
    - order_mode (optional): global ordering preference if no explicit OrderingRule is provided
    """

    spec_version: int = 1
    rules: List[Rule] = Field(default_factory=list)
    order_mode: Literal["flexible", "strict", "exact"] = "flexible"


# =====================
# Test Catalog (for UI pickers)
# =====================

# Expose operator names as strings for the catalog
AllowedMatcher = Literal[
    "text.contains",
    "text.not_contains",
    "text.equals",
    "text.regex",
    "number.cmp",
    "list.contains_any",
    "list.contains_all",
    "length.cmp",
]


ValueType = Literal[
    "text",
    "number",
    "list<string>",
    "list<object>",
    "object",
]


class FieldDescriptor(BaseModel):
    """Field inside a category (no JSON paths)."""

    key: str                      # e.g., "columns", "rows_count", "code", "text"
    label: str
    value_type: ValueType
    allowed_ops: List[AllowedMatcher]
    io: Optional[Literal["input", "output"]] = None  # for tool categories
    examples: Optional[List[Any]] = None
    # Optional select options for UI (e.g., model list). If provided, UI may
    # render a dropdown for single-value operators (e.g., text.equals).
    options: Optional[List[Dict[str, Any]]] = None


class CategoryDescriptor(BaseModel):
    id: str                       # "tool:create_widget" | "metadata" | "completion"
    label: str
    kind: Literal["tool", "metadata", "completion"]
    tool_name: Optional[str] = None
    fields: List[FieldDescriptor] = Field(default_factory=list)


class TestCatalog(BaseModel):
    categories: List[CategoryDescriptor] = Field(default_factory=list)


def default_test_catalog() -> TestCatalog:
    """Static, curated catalog for MVP. UI calls an endpoint to fetch this.

    Later, we can enrich/compose this from tool schemas.
    """

    categories: List[CategoryDescriptor] = []

    categories.append(CategoryDescriptor(
        id="tool:create_data",
        label="Create Data",
        kind="tool",
        tool_name="create_data",
        fields=[
            FieldDescriptor(
                key="tables",
                label="Used tables",
                value_type="list<string>",
                allowed_ops=["list.contains_any", "list.contains_all"],
                io="input",
                examples=[["table1", "table2"]],
            ),
            FieldDescriptor(
                key="columns",
                label="Columns — names",
                value_type="list<string>",
                allowed_ops=["list.contains_any", "list.contains_all"],
                io="output",
                examples=["amount", "order_date"],
            ),
            FieldDescriptor(
                key="rows_count",
                label="Rows count",
                value_type="number",
                allowed_ops=["number.cmp"],
                io="output",
                examples=[5],
            ),
            FieldDescriptor(
                key="code",
                label="Generated code",
                value_type="text",
                allowed_ops=["text.contains", "text.not_contains", "text.regex"],
                io="output",
                examples=["groupby", "agg"],
            )
        ]
    ))

    categories.append(CategoryDescriptor(
        id="tool:clarify",
        label="Clarify",
        kind="tool",
        tool_name="clarify",
        fields=[
            FieldDescriptor(
                key="question",
                label="Question",
                value_type="text",
                allowed_ops=["text.contains", "text.equals", "text.regex"],
                io="output",
                examples=[["What do you mean by 'total revenue'?"]],
            )
        ]
    ))

    categories.append(CategoryDescriptor(
        id="tool:describe_table",
        label="Describe Table",
        kind="tool",
        tool_name="describe_table",
        fields=[
            FieldDescriptor(
                key="name",
                label="Table name",
                value_type="text",
                allowed_ops=["text.equals", "text.contains"],
                io="input",
            )
        ]
    ))

    categories.append(CategoryDescriptor(
        id="metadata",
        label="Metadata",
        kind="metadata",
        fields=[
            FieldDescriptor(key="total_tokens", label="Total tokens", value_type="number", allowed_ops=["number.cmp"]),
            FieldDescriptor(key="total_duration_ms", label="Total duration (ms)", value_type="number", allowed_ops=["number.cmp"]),
            FieldDescriptor(key="total_iterations", label="Total iterations", value_type="number", allowed_ops=["number.cmp"]),
        ]
    ))

    categories.append(CategoryDescriptor(
        id="completion",
        label="Completion",
        kind="completion",
        fields=[
            FieldDescriptor(key="text", label="Completion text", value_type="text", allowed_ops=["text.contains", "text.equals", "text.regex"]),
            FieldDescriptor(key="reasoning", label="Reasoning", value_type="text", allowed_ops=["text.contains", "text.equals", "text.regex"]),
        ]
    ))

    # Judge / LLM Test (prompt + model selector). Model options are populated
    # dynamically in TestSuiteService.get_test_catalog
    categories.append(CategoryDescriptor(
        id="judge",
        label="Judge (LLM)",
        kind="metadata",
        fields=[
            FieldDescriptor(
                key="prompt",
                label="Judge prompt",
                value_type="text",
                allowed_ops=["text.equals"]
            ),
            FieldDescriptor(
                key="model_id",
                label="Judge model",
                value_type="text",
                allowed_ops=["text.equals"],
                options=None  # to be filled with available models by service
            ),
        ]
    ))

    return TestCatalog(categories=categories)



