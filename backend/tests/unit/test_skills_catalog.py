"""Pre-built skills catalog — parsing and installed-state invariants.

Covers:
  - the bundled catalog parses and exposes the shipped slides-creator entry
  - malformed entries are skipped rather than breaking the whole listing
  - required frontmatter is enforced
  - install() rejects an unknown key
  - installed state ignores soft-deleted rows (so a deleted skill can be
    reinstalled instead of being stuck as "Installed" forever)
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.skills_catalog_service import SkillsCatalogService


def _service_with_dir(tmp_path, monkeypatch) -> SkillsCatalogService:
    monkeypatch.setattr("app.services.skills_catalog_service.CATALOG_DIR", tmp_path)
    return SkillsCatalogService()


def _write_skill(root, name: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


class TestCatalogParsing:
    def test_bundled_slides_creator_is_listed(self):
        """The shipped catalog must expose slides-creator with a body."""
        entries = SkillsCatalogService().list_catalog()
        by_key = {e["key"]: e for e in entries}
        assert "slides-creator" in by_key, f"got keys: {list(by_key)}"
        entry = by_key["slides-creator"]
        assert entry["name"]
        assert entry["description"]
        # The body is what becomes the instruction text — it must not be empty.
        assert len(entry["text"]) > 200

    def test_malformed_entries_are_skipped_not_fatal(self, tmp_path, monkeypatch):
        svc = _service_with_dir(tmp_path, monkeypatch)
        _write_skill(tmp_path, "good", "---\nkey: good\nname: Good\ndescription: d\n---\nbody text")
        _write_skill(tmp_path, "nofrontmatter", "just a body, no frontmatter")
        _write_skill(tmp_path, "badyaml", "---\nkey: [unclosed\n---\nbody")
        _write_skill(tmp_path, "missingdesc", "---\nkey: m\nname: M\n---\nbody")

        keys = [e["key"] for e in svc.list_catalog()]
        assert keys == ["good"]

    def test_duplicate_keys_keep_first(self, tmp_path, monkeypatch):
        svc = _service_with_dir(tmp_path, monkeypatch)
        _write_skill(tmp_path, "a_dir", "---\nkey: dup\nname: A\ndescription: d\n---\nbody")
        _write_skill(tmp_path, "b_dir", "---\nkey: dup\nname: B\ndescription: d\n---\nbody")
        entries = svc.list_catalog()
        assert len(entries) == 1

    def test_defaults_are_applied(self, tmp_path, monkeypatch):
        svc = _service_with_dir(tmp_path, monkeypatch)
        _write_skill(tmp_path, "x", "---\nkey: x\nname: X\ndescription: d\n---\nbody")
        entry = svc.get_entry("x")
        assert entry["category"] == "general"
        assert entry["version"] == "1.0.0"
        assert entry["icon"]

    def test_get_entry_unknown_key(self):
        assert SkillsCatalogService().get_entry("does-not-exist") is None


class TestInstalledState:
    @pytest.mark.asyncio
    async def test_soft_deleted_rows_are_not_installed(self):
        """A deleted skill must read as not-installed so it can be reinstalled."""
        svc = SkillsCatalogService()
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        ))
        org = SimpleNamespace(id="org-1")

        installed = await svc._installed_rows(db, org)
        assert installed == {}

        # The query must constrain deleted_at — otherwise a soft-deleted row
        # would come back and block reinstalling forever.
        where_sql = str(db.execute.call_args[0][0])
        assert "deleted_at IS NULL" in where_sql

    @pytest.mark.asyncio
    async def test_list_with_state_marks_installed(self, tmp_path, monkeypatch):
        svc = _service_with_dir(tmp_path, monkeypatch)
        _write_skill(tmp_path, "x", "---\nkey: x\nname: X\ndescription: d\nversion: 2.0.0\n---\nbody")

        row = SimpleNamespace(id="instr-1", catalog_key="x", catalog_version="2.0.0")
        monkeypatch.setattr(svc, "_installed_rows", AsyncMock(return_value={"x": row}))

        items = await svc.list_with_state(MagicMock(), SimpleNamespace(id="org-1"))
        assert items[0]["installed"] is True
        assert items[0]["instruction_id"] == "instr-1"
        assert items[0]["installed_version"] == "2.0.0"
        # The list payload stays light — the body is not shipped to the browser.
        assert "text" not in items[0]

    @pytest.mark.asyncio
    async def test_install_unknown_key_raises(self):
        svc = SkillsCatalogService()
        with pytest.raises(ValueError):
            await svc.install(MagicMock(), "no-such-skill", MagicMock(), SimpleNamespace(id="o"))

    @pytest.mark.asyncio
    async def test_install_is_idempotent(self, tmp_path, monkeypatch):
        """Installing twice returns the existing row instead of duplicating."""
        svc = _service_with_dir(tmp_path, monkeypatch)
        _write_skill(tmp_path, "x", "---\nkey: x\nname: X\ndescription: d\n---\nbody")
        row = SimpleNamespace(id="instr-1", catalog_key="x", catalog_version="1.0.0")
        monkeypatch.setattr(svc, "_installed_rows", AsyncMock(return_value={"x": row}))

        result = await svc.install(MagicMock(), "x", MagicMock(), SimpleNamespace(id="o"))
        assert result == {"instruction_id": "instr-1", "already_installed": True}
