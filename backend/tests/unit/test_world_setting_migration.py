"""worldai01: the World toggle moves from general.show_world to a top-level
feature; orgs that had it on keep it on."""
import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "worldai01_move_show_world_to_ai_settings.py"


def _load():
    spec = importlib.util.spec_from_file_location("worldai01", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(monkeypatch, rows, step):
    mod = _load()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE organization_settings (id TEXT PRIMARY KEY, config JSON)"))
        for rid, cfg in rows.items():
            conn.execute(sa.text("INSERT INTO organization_settings VALUES (:i, :c)"), {"i": rid, "c": json.dumps(cfg)})
        monkeypatch.setattr(mod.op, "get_bind", lambda: conn, raising=False)
        getattr(mod, step)()
        return {r[0]: json.loads(r[1]) for r in conn.execute(sa.text("SELECT id, config FROM organization_settings"))}


def test_orgs_with_world_on_keep_it(monkeypatch):
    out = _run(monkeypatch, {
        "on": {"general": {"ai_analyst_name": "Ana", "show_world": True}},
        "off": {"general": {"ai_analyst_name": "Ana", "show_world": False}},
        "never-set": {"general": {"ai_analyst_name": "Ana"}},
        "no-general": {},
        "already-moved": {"general": {"show_world": True}, "show_world": {"value": False, "name": "World view", "description": "d"}},
    }, "upgrade")

    assert out["on"]["show_world"]["value"] is True
    assert out["on"]["show_world"]["state"] == "enabled"
    assert "show_world" not in out["off"]  # schema default (off) applies
    assert "show_world" not in out["never-set"]
    assert out["no-general"] == {}
    assert out["already-moved"]["show_world"]["value"] is False  # an explicit choice is kept
    # The old key is gone; the rest of general is untouched.
    for rid in ("on", "off", "already-moved"):
        assert "show_world" not in out[rid]["general"]
    assert out["on"]["general"] == {"ai_analyst_name": "Ana"}


def test_downgrade_restores_general_flag(monkeypatch):
    out = _run(monkeypatch, {
        "on": {"general": {"ai_analyst_name": "Ana"}, "show_world": {"value": True, "name": "World view", "description": "d"}},
        "off": {"show_world": {"value": False, "name": "World view", "description": "d"}},
        "untouched": {"general": {"ai_analyst_name": "Ana"}},
    }, "downgrade")

    assert out["on"] == {"general": {"ai_analyst_name": "Ana", "show_world": True}}
    assert out["off"] == {"general": {"show_world": False}}
    assert out["untouched"] == {"general": {"ai_analyst_name": "Ana"}}
