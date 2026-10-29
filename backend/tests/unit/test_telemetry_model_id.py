import pytest

from app.core.telemetry import telemetry_model_id


@pytest.mark.parametrize("model_id, expected", [
    # Catalog / provider ids pass through unchanged
    ("gpt-6-sol", "gpt-6-sol"),
    ("gpt-6.1-sol", "gpt-6.1-sol"),
    ("gpt-5.6-sol", "gpt-5.6-sol"),
    ("gpt-5.4-mini", "gpt-5.4-mini"),
    ("gpt-image-2.5-sunburst", "gpt-image-2.5-sunburst"),
    ("o4-mini", "o4-mini"),
    ("gpt-oss-120b", "gpt-oss-120b"),
    ("Claude-Sonnet-5-5", "claude-sonnet-5-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    ("gemini-3.1-pro-preview", "gemini-3.1-pro-preview"),
    # Bedrock / Azure / Ollama wrappers reduce to the model name
    ("us.anthropic.claude-sonnet-5-5-v1:0", "claude-sonnet-5-5"),
    ("anthropic.claude-opus-5-5", "claude-opus-5-5"),
    ("stmarys-gpt-6-sol-prod", "gpt-6-sol"),
    # Open-weight families
    ("deepseek-r1:70b", "deepseek-r1-70b"),
    ("deepseek-chat", "deepseek-chat"),
    ("DeepSeek-V3.2", "deepseek-v3.2"),
    ("llama3.3:70b", "llama3.3-70b"),
    ("Llama-3.3-70B-Instruct", "llama-3.3-70b-instruct"),
    ("meta-llama/Llama-4-Maverick-17B-128E-Instruct", "llama-4-maverick-17b-128e-instruct"),
    ("qwen2.5-coder:32b", "qwen2.5-coder-32b"),
    ("mistral-large-latest", "mistral-large-latest"),
    ("grok-4", "grok-4"),
    ("chatgpt-4o-latest", "chatgpt-4o-latest"),
    ("gpt-4o-mini", "gpt-4o-mini"),
    ("o5-pro", "o5-pro"),
    ("codellama:13b", "codellama-13b"),
    ("granite-3.3-8b-instruct", "granite-3.3-8b-instruct"),
    ("pixtral-large-latest", "pixtral-large-latest"),
    ("starcoder2:15b", "starcoder2-15b"),
    ("hermes3:8b", "hermes3-8b"),
    ("text-embedding-3-small", "text-embedding-3-small"),
    ("nomic-embed-text", "nomic-embed"),
    ("bge-m3", "bge-m3"),
    # Short families need a version digit
    ("yi-1.5-34b", "yi-1.5-34b"),
    ("amazon.nova-2-lite-v1:0", "nova-2-lite"),
    ("aya-23-8b", "aya-23-8b"),
])
def test_known_models_are_named(model_id, expected):
    assert telemetry_model_id(model_id) == expected


@pytest.mark.parametrize("model_id", [
    "stmarys-hospital-llm",
    "philips-prod",       # "phi" only matches as a whole word
    "acme-proto",         # "o" families need a digit
    "acme-nova-prod",     # short families need a digit
    "step-function-llm",
    "solar-west",
])
def test_unknown_ids_are_masked(model_id):
    assert telemetry_model_id(model_id) == "custom"


def test_customer_suffix_is_dropped():
    assert "stmarys" not in telemetry_model_id("deepseek-r1-stmarys-west")


def test_empty():
    assert telemetry_model_id(None) is None
