"""sbxweb01: orgs that had web fetch on keep `http` in generated code after
the Sandbox split; everyone else stays at the new default (off)."""
import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "sbxweb01_sandbox_web_fetch_setting.py"


def _load():
    spec = importlib.util.spec_from_file_location("sbxweb01", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _feature(value):
    return {"value": value, "name": "Web Fetch", "description": "d", "is_lab": False, "editable": True, "state": "enabled"}


def test_existing_web_fetch_orgs_keep_http_in_code(monkeypatch):
    mod = _load()
    engine = sa.create_engine("sqlite://")
    rows = {
        "on-top-level": {"enable_web_fetch": _feature(True)},
        "on-in-ai-features": {"ai_features": {"enable_web_fetch": _feature(True)}},
        "off": {"enable_web_fetch": _feature(False)},
        "never-set": {},
        "already-decided": {"enable_web_fetch": _feature(True),
                            "enable_sandbox_web_fetch": _feature(False)},
    }
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE organization_settings (id TEXT PRIMARY KEY, config JSON)"))
        for rid, cfg in rows.items():
            conn.execute(sa.text("INSERT INTO organization_settings VALUES (:i, :c)"), {"i": rid, "c": json.dumps(cfg)})
        monkeypatch.setattr(mod.op, "get_bind", lambda: conn, raising=False)
        mod.upgrade()
        out = {r[0]: json.loads(r[1]) for r in conn.execute(sa.text("SELECT id, config FROM organization_settings"))}

    def code_fetch(rid):
        entry = out[rid].get("enable_sandbox_web_fetch")
        return None if entry is None else entry["value"]

    assert code_fetch("on-top-level") is True
    assert code_fetch("on-in-ai-features") is True
    assert code_fetch("off") is None  # schema default (off) applies
    assert code_fetch("never-set") is None
    assert code_fetch("already-decided") is False  # an explicit choice is kept
    # Nothing else in the config is touched.
    assert out["on-top-level"]["enable_web_fetch"]["value"] is True
