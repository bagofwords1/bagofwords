"""Fixtures for agent-sandbox tests.

The feature ships dark: the deployment flag is unset in CI, so every test
that needs the feature asks for ``agent_sandboxes_on``.
"""
import pytest

from app.settings.config import settings as bow_settings


@pytest.fixture
def agent_sandboxes_on(monkeypatch):
    """Turn the deployment flag (BOW_AGENT_SANDBOXES_ENABLED) on for one test."""
    monkeypatch.setattr(bow_settings.bow_config.agent_sandboxes, "enabled", True)
    yield


@pytest.fixture
def agent_sandboxes_off(monkeypatch):
    """Pin the deployment flag off, whatever the environment says."""
    monkeypatch.setattr(bow_settings.bow_config.agent_sandboxes, "enabled", False)
    yield
