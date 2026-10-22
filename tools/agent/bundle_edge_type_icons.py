"""Bundle the data-source brand icons for the edge agent's admin UI (dev helper).

The logic lives in data_plane/data_edge_agent/admin/icons.py, which the Docker
build runs itself; this wrapper just points it at this checkout's paths. The
output dir is gitignored — the copies are a build artifact, and the admin
server bundles them on first start in a dev checkout anyway — so running this
by hand is only needed to refresh an already-bundled dir after an icon or
type_specs.json change.

    python tools/agent/bundle_edge_type_icons.py
"""
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "data_plane" / "data_edge_agent"
STATIC = AGENT / "admin" / "static"

if __name__ == "__main__":
    sys.argv = [
        sys.argv[0],
        "--frontend-public", str(ROOT / "frontend" / "public"),
        "--specs", str(AGENT / "data_sources" / "type_specs.json"),
        "--out", str(STATIC / "data_sources_icons"),
        "--manifest", str(STATIC / "type_icons.json"),
    ]
    runpy.run_path(str(AGENT / "admin" / "icons.py"), run_name="__main__")
