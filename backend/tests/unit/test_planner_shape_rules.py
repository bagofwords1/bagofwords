"""The legacy planner (prompt_builder, BOW_PLANNER=v2) must not tell the model
to return granular rows for grouped questions.

A customer reported that "sum of quantities by product in a date range" on
Power BI produced a raw pull of the whole population first and an aggregated
query second. The legacy prompt carried the rule "'by X' -> return granular
rows and let the viz layer aggregate", with "revenue by month" as the worked
example, which is exactly that behaviour. On Power BI a granular pull streams
every row through the executeQueries REST API, so the rule was the slow path.

The shape now follows the user's words: grouped asks aggregate in the query,
row asks return rows. This test pins the contract on the fallback builder.
"""
from app.ai.agents.planner.prompt_builder import PromptBuilder
from app.schemas.ai.planner import PlannerInput


def _prompt() -> str:
    return PromptBuilder.build_prompt(
        PlannerInput(user_message="total quantity by product for Q1", mode="chat")
    )


def test_legacy_planner_no_longer_prefers_granular_rows_for_grouped_asks():
    prompt = _prompt()
    assert "let the viz layer aggregate" not in prompt
    assert "Granular rows — let the viz layer aggregate" not in prompt


def test_legacy_planner_states_grouped_asks_aggregate_in_query():
    prompt = _prompt()
    assert "**Grouped questions**" in prompt
    assert "aggregate in the query" in prompt
    # Row-returning asks still get their rows - the fix is intent-driven, not
    # "aggregate everything".
    assert "**Row-returning questions**" in prompt
    assert "return the rows" in prompt
