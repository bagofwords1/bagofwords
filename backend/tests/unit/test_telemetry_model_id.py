from app.core.telemetry import telemetry_model_id


def test_catalog_ids_pass_through():
    assert telemetry_model_id("gpt-6-sol") == "gpt-6-sol"
    assert telemetry_model_id("gpt-5.6-sol") == "gpt-5.6-sol"
    assert telemetry_model_id("Claude-Sonnet-5-5") == "claude-sonnet-5-5"


def test_wrapped_ids_map_to_longest_catalog_id():
    assert telemetry_model_id("us.anthropic.claude-sonnet-5-5-v1:0") == "claude-sonnet-5-5"
    assert telemetry_model_id("stmarys-gpt-5.4-mini-prod") == "gpt-5.4-mini"


def test_unknown_ids_are_masked():
    assert telemetry_model_id("stmarys-hospital-llm") == "custom"
    assert telemetry_model_id(None) is None
