"""MCP table selection sees the organization's instructions in full.

The MCP data tools (create_data, inspect_data, create/edit_artifact) pick
tables with one LLM call before the coder runs. A rule such as "revenue lives
in fct_orders" must reach that call wherever it sits in the rendered
instructions — not only when it falls in the first couple of thousand chars.
The LLM is the stubbed boundary; the prompt it receives is the contract.
"""

import asyncio
import uuid

import pytest

from app.ai.tools.mcp import context as mcp_context


class _CapturingLLM:
    prompts: list = []

    def __init__(self, *args, **kwargs):
        pass

    def inference(self, prompt, **kwargs):
        _CapturingLLM.prompts.append(prompt)
        return '[{"data_source_id": "ds-1", "tables": ["fct_orders"]}]'


@pytest.mark.parametrize("rule_at", [0, 1_999, 2_500, 12_000, 60_000])
def test_table_selection_prompt_carries_every_instruction(monkeypatch, rule_at):
    monkeypatch.setattr(mcp_context, "LLM", _CapturingLLM)
    _CapturingLLM.prompts = []
    rule = f"RULE-{uuid.uuid4().hex[:8]}: revenue questions use fct_orders, never stg_orders."
    filler = "".join(f"- unrelated guidance line {i}\n" for i in range(rule_at // 25 + 1))[:rule_at]
    instructions = filler + rule + "\n- trailing guidance"

    tables = [
        {"data_source_id": "ds-1", "data_source_name": "warehouse", "table_name": name,
         "columns": ["id", "amount"], "description": None}
        for name in ("fct_orders", "stg_orders")
    ]
    selected = asyncio.run(mcp_context._select_tables_with_llm(
        model=object(), prompt="total revenue last month",
        instructions_text=instructions, resources_text="", all_tables=tables,
    ))

    assert selected == [{"data_source_id": "ds-1", "tables": ["fct_orders"]}]
    assert len(_CapturingLLM.prompts) == 1
    assert rule in _CapturingLLM.prompts[0]
    assert "- trailing guidance" in _CapturingLLM.prompts[0]
