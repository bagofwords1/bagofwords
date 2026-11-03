"""Admin-UI icon bundling (admin/icons.py).

The bundle is a build artifact assembled from the frontend's assets; these
pin the contract the Docker build and the dev fallback both rely on: every
type with an asset gets a copy and a manifest entry, types without one are
simply absent (monogram fallback), and a rebuild drops stale files.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..admin import icons


def _frontend(tmp_path: Path, files: dict[str, bytes]) -> Path:
    public = tmp_path / "frontend" / "public"
    for rel, data in files.items():
        p = public / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return public


def _specs(tmp_path: Path, types: list[str]) -> Path:
    p = tmp_path / "type_specs.json"
    p.write_text(json.dumps({"types": {t: {} for t in types}}))
    return p


def test_resolver_mirrors_the_frontend_icon_component():
    assert icons.resolve_asset("postgresql") == ("data_sources_icons", "postgresql.png")
    assert icons.resolve_asset("Postgres") == ("data_sources_icons", "postgresql.png")   # alias
    assert icons.resolve_asset("elasticsearch") == ("data_sources_icons", "elasticsearch.svg")  # override
    assert icons.resolve_asset("tableau") == ("icons", "tableau.png")                   # tool icon dir


def test_bundle_copies_assets_and_writes_manifest(tmp_path):
    public = _frontend(tmp_path, {
        "data_sources_icons/postgresql.png": b"pg",
        "data_sources_icons/elasticsearch.svg": b"<svg/>",
        "icons/tableau.png": b"tb",
    })
    specs = _specs(tmp_path, ["postgresql", "elasticsearch", "tableau", "no_such_type"])
    out, manifest_path = tmp_path / "out", tmp_path / "type_icons.json"

    manifest = icons.bundle(public, specs, out, manifest_path)

    assert manifest == {"postgresql": "postgresql.png", "elasticsearch": "elasticsearch.svg",
                        "tableau": "tableau.png"}
    assert json.loads(manifest_path.read_text()) == manifest
    assert (out / "postgresql.png").read_bytes() == b"pg"
    assert (out / "tableau.png").read_bytes() == b"tb"          # flattened from icons/
    assert "no_such_type" not in manifest                       # monogram fallback


def test_rebundle_drops_stale_files_and_tracks_asset_changes(tmp_path):
    public = _frontend(tmp_path, {"data_sources_icons/postgresql.png": b"v1",
                                  "data_sources_icons/mysql.png": b"my"})
    out, manifest_path = tmp_path / "out", tmp_path / "type_icons.json"
    icons.bundle(public, _specs(tmp_path, ["postgresql", "mysql"]), out, manifest_path)

    (public / "data_sources_icons" / "postgresql.png").write_bytes(b"v2")  # frontend icon changed
    icons.bundle(public, _specs(tmp_path, ["postgresql"]), out, manifest_path)  # mysql dropped

    assert (out / "postgresql.png").read_bytes() == b"v2"
    assert not (out / "mysql.png").exists()
    assert "mysql" not in json.loads(manifest_path.read_text())
