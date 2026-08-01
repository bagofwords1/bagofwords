"""Unit tests for the relative-time-filter policy in codegen prompts.

Generated step code is saved and re-executed verbatim later (dashboard
refresh, scheduled runs — step_service.rerun_step), so a relative ask
("yesterday", "latest day") frozen into a literal date returns permanently
stale data on every rerun. These tests pin:

* the shared time-filter rules section in the Coder's codegen prompts,
* the per-dialect ``relative_date_hint`` riding along with each client's
  description in the <connection_clients> block (all four assembly sites
  render through ``render_ds_client_entry``),
* the planner-facing guidance (interpreted_prompt schema field and the
  create_data / inspect_data tool descriptions).
"""
import pytest

from app.ai.agents.coder.coder import Coder, _time_filter_rules
from app.ai.prompt_formatters import render_ds_client_entry
from app.ai.schemas.codegen import CodeGenContext


class _StubSettings:
    def get_config(self, key, default=None):
        return default


class _StubLLM:
    def __init__(self):
        self.prompts = []

    def inference(self, text, **kwargs):
        self.prompts.append(text)
        return "def generate_df(ds_clients, excel_files):\n    return df"

    async def inference_stream_v2(self, messages, **kwargs):
        self.prompts.append(messages[0].content)
        return
        yield  # pragma: no cover - makes this an async generator


class _StubCodeContextBuilder:
    async def get_top_successful_snippets_for_data_model(self, data_model):
        return ""

    async def get_top_failed_snippets_for_data_model(self, data_model):
        return ""


class _StubClient:
    description = "TestDB database at localhost:5432/test"
    relative_date_hint = (
        "Relative dates (TestSQL): CURRENT_DATE, CURRENT_DATE - INTERVAL '7 days'."
    )


class _HintlessClient:
    description = "Mailbox client for test@example.com"
    # No relative_date_hint attribute at all — like a client predating the hook.


def _make_coder() -> Coder:
    coder = Coder.__new__(Coder)
    coder.llm = _StubLLM()
    coder.organization_settings = _StubSettings()
    coder.enable_llm_see_data = True
    coder.instruction_context_builder = None
    coder.context_hub = None
    return coder


def _codegen_context(**kwargs) -> CodeGenContext:
    base = {"user_prompt": "total sales yesterday", "schemas_excerpt": "<schemas/>"}
    base.update(kwargs)
    return CodeGenContext(**base)


# ---------------------------------------------------------------------------
# The shared rules section itself
# ---------------------------------------------------------------------------

def test_time_filter_rules_forbid_frozen_literals_and_offer_alternatives():
    rules = _time_filter_rules()
    assert "RE-EXECUTED" in rules
    assert "NEVER freeze a relative ask into a literal date" in rules
    # Both dynamic strategies are named: data-relative and clock-relative.
    assert "SELECT MAX(order_date)" in rules
    assert "relative date functions" in rules
    # The explicit-fixed-date exception stays.
    assert "ONLY when the user explicitly named a fixed date" in rules
    # Outranks hardcoded-date patterns in reused snippets.
    assert "OUTRANKS" in rules


# ---------------------------------------------------------------------------
# Codegen prompts carry the rules (and drop the old resolve-to-literal wording)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_code_prompt_carries_time_filter_rules():
    coder = _make_coder()
    await coder.generate_code(
        data_model=None,
        prompt="total sales yesterday",
        interpreted_prompt="sum sales for yesterday",
        schemas="<schemas/>",
        ds_clients={},
        excel_files=[],
        code_and_error_messages=[],
        memories="",
        previous_messages=[],
        retries=0,
        context=_codegen_context(),
    )
    prompt = coder.llm.prompts[0]
    assert "NEVER freeze a relative ask into a literal date" in prompt
    assert "do NOT bake the resolved dates into the code as literals" in prompt
    # The old wording told the model to resolve "yesterday" into a literal.
    assert "Resolve relative date expressions" not in prompt


@pytest.mark.asyncio
async def test_data_model_to_code_prompt_carries_time_filter_rules():
    coder = _make_coder()
    await coder.data_model_to_code(
        data_model={"columns": []},
        prompt="total sales yesterday",
        schemas="<schemas/>",
        ds_clients={},
        excel_files=[],
        code_and_error_messages=[],
        memories="",
        previous_messages=[],
        retries=0,
        prev_data_model_code_pair=None,
        code_context_builder=_StubCodeContextBuilder(),
    )
    prompt = coder.llm.prompts[0]
    assert "NEVER freeze a relative ask into a literal date" in prompt
    assert "Resolve relative date expressions" not in prompt


@pytest.mark.asyncio
async def test_generate_transform_code_prompt_carries_time_filter_rules():
    coder = _make_coder()
    await coder.generate_transform_code(
        prompt="export sales for the last 7 days",
        schemas="<schemas/>",
        ds_clients={},
        excel_files=[],
        code_and_error_messages=[],
        memories="",
        previous_messages=[],
        retries=0,
        context=_codegen_context(),
    )
    prompt = coder.llm.prompts[0]
    assert "NEVER freeze a relative ask into a literal date" in prompt


@pytest.mark.asyncio
async def test_inspection_prompt_prefers_relative_dates_and_freshness_checks():
    coder = _make_coder()
    await coder.generate_inspection_code(
        prompt="check the latest day of sales",
        schemas="<schemas/>",
        ds_clients={},
        excel_files=[],
        code_and_error_messages=[],
        memories="",
        previous_messages=[],
        retries=0,
        context=_codegen_context(),
    )
    prompt = coder.llm.prompts[0]
    assert "Date coverage and freshness" in prompt
    assert "instead of hardcoding a discovered date" in prompt
    assert "**Relative dates**" in prompt


# ---------------------------------------------------------------------------
# Per-client relative_date_hint rendering (<connection_clients>)
# ---------------------------------------------------------------------------

def test_render_ds_client_entry_appends_hint():
    entry = render_ds_client_entry("Sales:testdb-1", _StubClient())
    assert "client_key: Sales:testdb-1" in entry
    assert "description: TestDB database" in entry
    assert "relative_dates: Relative dates (TestSQL)" in entry


def test_render_ds_client_entry_omits_hint_when_absent():
    entry = render_ds_client_entry("Mail:gmail-1", _HintlessClient())
    assert "description: Mailbox client" in entry
    assert "relative_dates:" not in entry


@pytest.mark.asyncio
async def test_connection_clients_block_carries_hint_in_coder_prompts():
    """The Coder's own loops (non-generate_code paths) render the hint too."""
    coder = _make_coder()
    ds_clients = {"Sales:testdb-1": _StubClient()}
    await coder.generate_inspection_code(
        prompt="check date ranges",
        schemas="<schemas/>",
        ds_clients=ds_clients,
        excel_files=[],
        code_and_error_messages=[],
        memories="",
        previous_messages=[],
        retries=0,
        context=_codegen_context(),
    )
    assert "relative_dates: Relative dates (TestSQL)" in coder.llm.prompts[0]


@pytest.mark.asyncio
async def test_build_codegen_context_carries_hint_for_generate_code():
    """generate_code renders <connection_clients> from data_sources_context,
    which build_codegen_context assembles through the same renderer."""
    from app.ai.prompt_formatters import build_codegen_context

    ctx = await build_codegen_context(
        runtime_ctx={"ds_clients": {"Sales:testdb-1": _StubClient()}},
        user_prompt="total sales yesterday",
        interpreted_prompt=None,
        schemas_excerpt="<schemas/>",
    )
    assert "relative_dates: Relative dates (TestSQL)" in ctx.data_sources_context


def test_base_client_defaults_to_no_hint():
    from app.data_sources.clients.base import DataSourceClient

    assert DataSourceClient.relative_date_hint is None


def test_sqlite_client_declares_dialect_hint():
    from app.data_sources.clients.sqlite_client import SqliteClient

    hint = SqliteClient.relative_date_hint
    assert hint and "date('now')" in hint


# ---------------------------------------------------------------------------
# Planner-facing guidance
# ---------------------------------------------------------------------------

def test_interpreted_prompt_schema_demands_relative_windows():
    from app.ai.tools.schemas.create_data import CreateDataInput

    desc = CreateDataInput.model_fields["interpreted_prompt"].description
    assert "RELATIVELY" in desc
    assert "re-executed verbatim" in desc


def test_create_data_tool_description_mentions_rerun_semantics():
    from app.ai.tools.implementations.create_data import CreateDataTool

    desc = CreateDataTool.__new__(CreateDataTool).metadata.description
    assert "re-executed verbatim" in desc
    assert "never as resolved literal dates" in desc


def test_inspect_data_tool_description_warns_against_baking_dates():
    from app.ai.tools.implementations.inspect_data import InspectDataTool

    desc = InspectDataTool.__new__(InspectDataTool).metadata.description
    assert "do NOT bake a discovered" in desc
