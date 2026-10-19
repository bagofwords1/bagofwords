"""The planner's focused schema block keeps the built-in BOW section.

When focus is on (many agents, or an explicit report focus), the schema block
renders only the focused agents. The built-in BOW section — where Agent Lists
live as ``bow.<agent>.lists.<list>`` tables carrying their ``list_id`` — is
not an agent, so it can never be "focused". It must still render, or an agent
filling a list through its submit tool has no way to read the list back.
"""
from types import SimpleNamespace

import pytest

from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.prompt_formatters import Table, TableColumn
from app.schemas.data_source_schema import DataSourceSummarySchema

LIST_ID = "3f0c8a52-7d0e-4a8b-9f36-2b7f1c0d9e41"


def _agent_section(i):
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id=f"agent-{i}", name=f"Agent {i}", type="postgresql"),
        tables=[Table(name=f"t_{i}", columns=[TableColumn(name="id", dtype="int")], pks=[], fks=[])],
    )


def _bow_section():
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="builtin:bow", name="BOW", type="bow"),
        tables=[Table(
            name="bow.share.lists.folders", columns=[TableColumn(name="folder", dtype="string")],
            pks=[], fks=[], description=f'Query via {{"dataset": "list", "list_id": "{LIST_ID}"}}',
        )],
    )


def _planner(n_agents, focused):
    from app.ai.agent_v2 import AgentV2

    agent = object.__new__(AgentV2)
    agent.data_sources = [SimpleNamespace(id=f"agent-{i}", name=f"Agent {i}", description="x", context=None,
                                          publish_status="published", primary_instruction_id=None)
                          for i in range(n_agents)]
    agent.report = SimpleNamespace(focused_data_source_ids=focused, data_sources=[])
    agent.organization = SimpleNamespace(id="org")
    agent.organization_settings = SimpleNamespace(get_config=lambda k: SimpleNamespace(value=10))
    agent.head_completion = None
    agent.db = None
    agent.loaded_agent_ids = ()
    agent.top_k_schema = 10
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("n_agents", [2, 8])
async def test_focused_schema_block_keeps_list_tables(n_agents):
    agent = _planner(n_agents, focused=["agent-0"])
    ctx = TablesSchemaContext(data_sources=[_agent_section(i) for i in range(n_agents)] + [_bow_section()])
    excerpt, _ = await agent._render_schemas_with_roster(ctx)
    assert "agent-0" in excerpt
    assert "agent-1" not in excerpt  # focus still narrows the agents
    assert LIST_ID in excerpt
