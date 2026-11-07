"""Which instructions apply in an MCP report, resolved one way for every tool.

get_context lists them, read_instruction (with report_id) only opens them, and
the data tools (create_data, inspect_data, create/edit_artifact) hand the ones
the client read to their code writer. All three go through these helpers so
the list a client sees is the set the server applies.
"""

from typing import Any, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
from app.models.organization import Organization
from app.models.user import User

# How the MCP data tools' ContextHub resolves the delivery channel: it is built
# without a head completion, so it uses 'app' (see ContextHub._init_builders).
# Kept identical here so get_context lists what those tools apply.
MCP_INSTRUCTION_CHANNEL = "app"


async def report_data_sources(db: AsyncSession, report, user: User, organization: Organization) -> List[Any]:
    """The report's agents this user may see and query — what get_context and
    the data tools work with."""
    from app.services.data_source_service import DataSourceService

    ds_service = DataSourceService()
    visible = await ds_service.filter_user_visible_data_sources(db, report.data_sources, user, organization)
    usable, _skipped = await ds_service.filter_user_usable_data_sources(db, visible, user)
    return usable


def instruction_builder(
    db: AsyncSession,
    organization: Organization,
    user: User,
    report,
    data_sources: List[Any],
    organization_settings: Optional[Any] = None,
) -> InstructionContextBuilder:
    """A builder scoped to the report: global instructions plus those on its
    agents (an explicit list, so a report with no agents gets global ones only),
    filtered by status, mode/channel and the user's table access."""
    return InstructionContextBuilder(
        db,
        organization,
        current_user=user,
        organization_settings=organization_settings,
        data_source_ids=[str(ds.id) for ds in data_sources],
        mode=getattr(report, "mode", None),
        channel=MCP_INSTRUCTION_CHANNEL,
    )


def in_scope(instruction_agent_ids, report_agent_ids) -> bool:
    """Global instructions (no agents) apply everywhere; the rest only where
    one of their agents is in the report."""
    agents = {str(a) for a in instruction_agent_ids or []}
    return not agents or bool(agents & {str(a) for a in report_agent_ids})
