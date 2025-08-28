"""
ContextView - Read-only consumer-facing view over the current context snapshot.

Object-based grouping that separates static vs warm sections for planner/tools.
"""
from pydantic import BaseModel
from typing import Optional

from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.context.sections.files_schema_section import FilesSchemaContext
from app.ai.context.sections.instructions_section import InstructionsSection
from app.ai.context.sections.messages_section import MessagesSection
from app.ai.context.sections.widgets_section import WidgetsSection
from app.ai.context.sections.observations_section import ObservationsSection
from app.ai.context.sections.resources_section import ResourcesSection
from app.ai.context.sections.code_section import CodeSection


class StaticSections(BaseModel):
    schemas: Optional[TablesSchemaContext] = None
    instructions: Optional[InstructionsSection] = None
    resources: Optional[ResourcesSection] = None
    code: Optional[CodeSection] = None
    files: Optional[FilesSchemaContext] = None


class WarmSections(BaseModel):
    messages: Optional[MessagesSection] = None
    observations: Optional[ObservationsSection] = None
    widgets: Optional[WidgetsSection] = None


class ContextView(BaseModel):
    static: StaticSections
    warm: WarmSections
    meta: dict = {}

