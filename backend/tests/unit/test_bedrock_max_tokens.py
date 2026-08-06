"""Bedrock requests must carry an explicit output-token budget.

Without inferenceConfig.maxTokens, Bedrock falls back to the model provider's
default cap — far below what codegen needs, which surfaced as generated code
truncated mid-string ("unterminated string literal").
"""
from __future__ import annotations

from app.ai.llm.clients.bedrock_client import (
    _DEFAULT_BEDROCK_MAX_TOKENS,
    _bedrock_max_tokens,
)


def test_default_budget(monkeypatch):
    monkeypatch.delenv("BOW_BEDROCK_MAX_TOKENS", raising=False)
    assert _bedrock_max_tokens() == _DEFAULT_BEDROCK_MAX_TOKENS


def test_env_override(monkeypatch):
    monkeypatch.setenv("BOW_BEDROCK_MAX_TOKENS", "32768")
    assert _bedrock_max_tokens() == 32768


def test_garbage_env_falls_back(monkeypatch):
    monkeypatch.setenv("BOW_BEDROCK_MAX_TOKENS", "not-a-number")
    assert _bedrock_max_tokens() == _DEFAULT_BEDROCK_MAX_TOKENS
    monkeypatch.setenv("BOW_BEDROCK_MAX_TOKENS", "-5")
    assert _bedrock_max_tokens() == _DEFAULT_BEDROCK_MAX_TOKENS
