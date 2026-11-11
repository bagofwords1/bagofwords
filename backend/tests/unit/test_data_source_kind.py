"""Agent vs connector — the contract.

* Each person's own accounts/tools (mail, Teams, personal Drive, MCP apps) are
  connectors; things the org curates for analysis (databases, BI, shared file
  catalogs, browser, custom APIs, a Drive service account) are agents.
* An explicit kind (create flow / admin) always wins; NULL derives it.
* A run keeps its AGENT scope (pinned or Auto). Auto adds every connector the
  user can reach; a pinned selection adds only the user's OWN connectors (plus
  any connector pinned on the report). A pinned selection is used as pinned.
* A scheduled run whose output reaches other people never reads the owner's
  personal connectors.
* Connectors render as their own <connectors> block, never inside <agents>.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.data_source_kind import AGENT, CONNECTOR, data_source_kind, derived_kind


def _conn(type_, auth_policy="system_only"):
    return SimpleNamespace(type=type_, auth_policy=auth_policy)


def _ds(*conns, kind=None, id_="ds", name="DS"):
    return SimpleNamespace(id=id_, name=name, kind=kind, connections=list(conns), is_active=True,
                           publish_status="published", is_public=True)


@pytest.mark.parametrize("conns,expected", [
    # each person's own account or tools
    ([_conn("gmail_mail", "user_required")], CONNECTOR),
    ([_conn("outlook_mail", "user_required")], CONNECTOR),
    ([_conn("teams_messages", "user_required")], CONNECTOR),
    ([_conn("google_drive", "user_required")], CONNECTOR),
    ([_conn("onedrive", "user_required")], CONNECTOR),
    ([_conn("mcp", "user_required")], CONNECTOR),
    ([_conn("mcp", "system_only")], CONNECTOR),          # shared-key MCP: still a tool connector
    # the org's curated sources
    ([_conn("google_drive", "system_only")], AGENT),     # service account = shared catalog
    ([_conn("onenote", "user_required")], AGENT),        # shared team notebook catalog
    ([_conn("sharepoint", "system_only")], AGENT),
    ([_conn("postgresql")], AGENT),
    ([_conn("snowflake", "user_required")], AGENT),      # per-user login, shared schema
    ([_conn("browser")], AGENT),
    ([_conn("custom_api")], AGENT),
    # mixed: anything analysable makes it an agent
    ([_conn("postgresql"), _conn("gmail_mail", "user_required")], AGENT),
    ([], AGENT),
])
def test_derived_kind_follows_whose_data_it_is(conns, expected):
    assert derived_kind(_ds(*conns)) == expected


@pytest.mark.parametrize("explicit", [AGENT, CONNECTOR])
def test_an_explicit_kind_always_wins(explicit):
    opposite = [_conn("postgresql")] if explicit == CONNECTOR else [_conn("mcp", "user_required")]
    assert data_source_kind(_ds(*opposite, kind=explicit)) == explicit


def test_an_unknown_stored_value_falls_back_to_the_derived_kind():
    assert data_source_kind(_ds(_conn("gmail_mail", "user_required"), kind="weird")) == CONNECTOR


# ----------------------------------------------------------- run scope


def _patch_scope(monkeypatch, accessible):
    from app.ai.tools.implementations import agent_focus_common as afc

    async def _accessible(db, organization, user):
        return list(accessible)

    async def _filter(db, agents, org_id):
        return agents
    monkeypatch.setattr(afc, "accessible_agents", _accessible)
    monkeypatch.setattr("app.services.connected_application_service.filter_agents", _filter)
    monkeypatch.setattr("app.services.data_source_service.DataSourceService.is_execution_live",
                        staticmethod(lambda ds: True))
    return afc


SNOWFLAKE = _ds(_conn("snowflake"), id_="snow", name="Snowflake")
BI = _ds(_conn("powerbi"), id_="bi", name="BI")
TEAMS = _ds(_conn("teams_messages", "user_required"), id_="teams", name="Teams")
NOTION_SHARED = _ds(_conn("mcp", "system_only"), id_="notion", name="Notion")


@pytest.mark.asyncio
async def test_a_pinned_agent_selection_still_gets_the_users_own_connectors(monkeypatch):
    afc = _patch_scope(monkeypatch, [SNOWFLAKE, BI, TEAMS, NOTION_SHARED])
    report = SimpleNamespace(data_sources=[SNOWFLAKE], report_type="regular")
    ids = [d.id for d in await afc.resolve_run_agents(None, SimpleNamespace(id="o"), None, report)]
    # BI is not pinned; a shared-key MCP follows the selection, so pinning
    # still narrows the tools a run offers.
    assert ids[0] == "snow" and set(ids) == {"snow", "teams"}


@pytest.mark.asyncio
async def test_a_shared_connector_pinned_beside_an_agent_stays(monkeypatch):
    afc = _patch_scope(monkeypatch, [SNOWFLAKE, BI, TEAMS, NOTION_SHARED])
    report = SimpleNamespace(data_sources=[SNOWFLAKE, NOTION_SHARED], report_type="regular")
    ids = {d.id for d in await afc.resolve_run_agents(None, SimpleNamespace(id="o"), None, report)}
    assert ids == {"snow", "notion", "teams"}


@pytest.mark.asyncio
async def test_auto_keeps_shared_connectors(monkeypatch):
    afc = _patch_scope(monkeypatch, [SNOWFLAKE, TEAMS, NOTION_SHARED])
    report = SimpleNamespace(data_sources=[], report_type="regular")
    ids = {d.id for d in await afc.resolve_run_agents(None, SimpleNamespace(id="o"), None, report)}
    assert ids == {"snow", "teams", "notion"}


@pytest.mark.asyncio
async def test_a_pin_on_a_shared_connector_scopes_the_run_like_any_pin(monkeypatch):
    # An old report that selected an MCP "agent" keeps that narrow tool set.
    afc = _patch_scope(monkeypatch, [SNOWFLAKE, BI, TEAMS, NOTION_SHARED])
    report = SimpleNamespace(data_sources=[NOTION_SHARED], report_type="regular")
    ids = {d.id for d in await afc.resolve_run_agents(None, SimpleNamespace(id="o"), None, report)}
    assert ids == {"notion", "teams"}


@pytest.mark.asyncio
async def test_runs_that_reach_others_drop_personal_connectors_only(monkeypatch):
    afc = _patch_scope(monkeypatch, [SNOWFLAKE, TEAMS, NOTION_SHARED])
    report = SimpleNamespace(data_sources=[], report_type="regular")
    ids = {d.id for d in await afc.resolve_run_agents(None, SimpleNamespace(id="o"), None, report,
                                                     exclude_personal_connectors=True)}
    assert ids == {"snow", "notion"}


@pytest.mark.asyncio
@pytest.mark.parametrize("subscribers,reaches", [
    (None, False),
    ([], False),
    ([{"type": "user", "id": "owner"}], False),
    ([{"type": "user", "id": "owner"}, {"type": "user", "id": "lee"}], True),
    ([{"type": "email", "address": "board@x.test"}], True),
    ([{"type": "slack", "id": "C123"}], True),
])
async def test_scheduled_output_reaching_anyone_but_the_owner_is_detected(subscribers, reaches):
    from app.ai.tools.implementations.agent_focus_common import scheduled_run_reaches_others

    class _DB:
        async def get(self, model, key):
            return SimpleNamespace(user_id="owner", notification_subscribers=subscribers)
    assert await scheduled_run_reaches_others(_DB(), "sp-1", "owner") is reaches
    assert await scheduled_run_reaches_others(_DB(), None, "owner") is False


# ----------------------------------------------------------- context


def test_connectors_render_as_their_own_block_not_as_agents():
    from app.ai.context.sections.tables_schema_section import FileScopeItem, TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema

    teams = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="teams", name="Microsoft Teams", type="teams_messages"),
        tables=[],
        file_scopes=[FileScopeItem(connection_id="c-teams", name="Microsoft Teams", type="teams_messages",
                                   base=None, file_count=0, sample=[], index_mode="content",
                                   supports_search=True, per_user=True, enforces_scope=False)],
    )
    out = TablesSchemaContext(data_sources=[teams]).render_connectors()
    assert out.startswith("<connectors") and "<connector " in out
    assert "<agents" not in out and "<data_source" not in out
    assert "search_chat_messages" in out
    assert TablesSchemaContext(data_sources=[]).render_connectors() == ""
