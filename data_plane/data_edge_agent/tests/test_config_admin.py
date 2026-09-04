"""Config surface added for the admin UI (design C4): defaults + env coercion."""

from __future__ import annotations

import pytest

from ..config import AgentConfig, load_config


def test_admin_defaults():
    cfg = AgentConfig(org_id="cust-b", edge_agent_id="nyc-01")
    assert cfg.admin_enabled is True
    assert cfg.admin_host == "127.0.0.1"   # loopback by design
    assert cfg.admin_port == 9191
    assert cfg.store_path is None


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("1", True), ("true", True), ("yes", True), ("ON", True),
])
def test_admin_enabled_env_coercion(monkeypatch, raw, expected):
    monkeypatch.setenv("BOW_EDGE_AGENT_ORG_ID", "cust-b")
    monkeypatch.setenv("BOW_EDGE_AGENT_EDGE_AGENT_ID", "nyc-01")
    monkeypatch.setenv("BOW_EDGE_AGENT_ADMIN_ENABLED", raw)
    assert load_config(None).admin_enabled is expected


def test_admin_and_store_env_overrides(monkeypatch):
    monkeypatch.setenv("BOW_EDGE_AGENT_ORG_ID", "cust-b")
    monkeypatch.setenv("BOW_EDGE_AGENT_EDGE_AGENT_ID", "nyc-01")
    monkeypatch.setenv("BOW_EDGE_AGENT_ADMIN_HOST", "0.0.0.0")
    monkeypatch.setenv("BOW_EDGE_AGENT_ADMIN_PORT", "9292")
    monkeypatch.setenv("BOW_EDGE_AGENT_STORE_PATH", "/data/store.json")
    cfg = load_config(None)
    assert cfg.admin_host == "0.0.0.0"
    assert cfg.admin_port == 9292          # int-coerced
    assert cfg.store_path == "/data/store.json"
