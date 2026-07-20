from typing import List, Optional
from pydantic import BaseModel, Field


class CreateDocInput(BaseModel):
    """Input for create_doc tool.

    - title: document title shown to end users
    - markdown: the full document body in markdown
    """

    title: str = Field(..., description=(
        "Document title. Concise and descriptive for end users, in the same language as the user/prompt."
    ))
    markdown: str = Field(..., description=(
        "The full document body in markdown. You author this directly — write polished, "
        "well-structured analytical prose.\n\n"
        "Supported building blocks:\n"
        "- Standard markdown: headings, paragraphs, **bold**, lists, tables, blockquotes, code fences.\n"
        "- Live charts: embed a visualization with {{viz:<uuid>}} on its own line. Use viz_ids from "
        "previous create_data results ('viz_id: <uuid>'). The chart renders live with current data — "
        "do NOT paste the chart's rows as a markdown table next to it.\n"
        "- Diagrams: fenced ```mermaid blocks for flow/causal/sequence diagrams. In flowcharts, "
        "ALWAYS wrap a node label in double quotes when it contains punctuation such as "
        "parentheses, colons or brackets — e.g. E[\"revenue SUM(Invoice.Total)\"], not "
        "E[revenue SUM(Invoice.Total)]. Unquoted punctuation breaks the diagram parser.\n"
        "- Multi-column layout: wrap content in ::: columns ... ::: with ::: col dividers between columns.\n\n"
        "Analytical writing standards (follow strictly):\n"
        "- CITATIONS: every number, trend or conclusion names its source — the table/column queried, the "
        "embedded viz it comes from, and the time range covered. Findings without a source do not go in "
        "the doc. Distinguish 'data shows X' from 'inferred X'.\n"
        "- Charts carry the evidence; prose carries the interpretation. State headline numbers inline but "
        "never restate a chart's full rows in text.\n"
        "- Structure follows the analytical genre (root-cause analysis, deep-dive report, executive memo, "
        "data audit) — see the planner guidance for the section skeletons.\n"
        "- Write in the user's language."
    ))


class CreateDocOutput(BaseModel):
    """Output from create_doc tool."""

    doc_id: str = Field(..., description="ID of the created document artifact")
    title: str = Field(..., description="Document title")
    version: int = Field(default=1, description="Version number of the document")
    visualization_ids: List[str] = Field(default_factory=list, description=(
        "All visualization IDs embedded in the document. Use these when editing with edit_doc."
    ))
    outline: List[str] = Field(default_factory=list, description="Heading outline of the document")
