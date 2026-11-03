"""Start-up wiring for the admin store (design C4): path resolution + merge."""

from __future__ import annotations

from pathlib import Path


from ..config import AgentConfig, ConnectionConfig
from ..main import _merge_store_connections, _resolve_store_path
from ..store import ConnectionStore


def _cfg(**over) -> AgentConfig:
    base = dict(org_id="cust-b", edge_agent_id="nyc-01")
    base.update(over)
    return AgentConfig(**base)


# -- _resolve_store_path -----------------------------------------------------

def test_store_path_explicit_wins():
    cfg = _cfg(store_path="/var/lib/bow/store.json")
    assert _resolve_store_path(cfg, "/etc/agent.yaml") == Path("/var/lib/bow/store.json")


def test_store_path_beside_config_when_not_set(tmp_path):
    cfg = _cfg()
    cfg_file = tmp_path / "agent.yaml"
    got = _resolve_store_path(cfg, str(cfg_file))
    assert got == tmp_path.resolve() / "edge-agent-store.json"


def test_store_path_falls_back_to_cwd_when_config_is_env_only():
    cfg = _cfg()
    got = _resolve_store_path(cfg, None)
    assert got == Path.cwd() / "edge-agent-store.json"


# -- _merge_store_connections ------------------------------------------------

def _store(tmp_path, *conns) -> ConnectionStore:
    s = ConnectionStore(tmp_path / "store.json")
    for c in conns:
        s.upsert(c)
    return s


def test_merge_store_wins_by_name(tmp_path):
    cfg = _cfg(connections=[
        ConnectionConfig(name="pg", type="postgresql", config={"host": "from-file"}),
        ConnectionConfig(name="only-file", type="postgresql"),
    ])
    store = _store(tmp_path, ConnectionConfig(
        name="pg", type="postgresql", config={"host": "from-store"},
        credentials={"user": "u"}))

    _merge_store_connections(cfg, store)

    by_name = {c.name: c for c in cfg.connections}
    assert by_name["pg"].config["host"] == "from-store"      # store wins
    assert by_name["pg"].credentials == {"user": "u"}
    assert "only-file" in by_name                            # file-only kept
    assert len(cfg.connections) == 2


def test_merge_adds_store_only_connections(tmp_path):
    cfg = _cfg(connections=[ConnectionConfig(name="file-pg", type="postgresql")])
    store = _store(tmp_path, ConnectionConfig(name="ui-mysql", type="mysql"))

    _merge_store_connections(cfg, store)

    assert sorted(c.name for c in cfg.connections) == ["file-pg", "ui-mysql"]


def test_merge_empty_store_is_a_noop(tmp_path):
    original = [ConnectionConfig(name="pg", type="postgresql")]
    cfg = _cfg(connections=list(original))
    _merge_store_connections(cfg, _store(tmp_path))
    assert [c.name for c in cfg.connections] == ["pg"]
