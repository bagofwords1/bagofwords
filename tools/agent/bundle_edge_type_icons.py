"""Bundle the data-source brand icons the edge agent's admin UI needs.

The standalone admin SPA can't reach the Bow frontend's assets at runtime, so we
copy the icons for the catalog's types into the agent's static dir and write a
`{type: filename}` manifest the SPA renders from. The type→file resolution
mirrors frontend/components/DataSourceIcon.vue (normalizeType + the explicit
overrides) so the icons match what Bow shows.

    python tools/agent/bundle_edge_type_icons.py

Run after regenerating type_specs.json (new types) or when frontend icons change.
Types with no matching asset simply fall back to the monogram tile in the SPA.
"""
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_PUBLIC = ROOT / "frontend" / "public"
SPECS = ROOT / "data_plane" / "data_edge_agent" / "data_sources" / "type_specs.json"
OUT_DIR = ROOT / "data_plane" / "data_edge_agent" / "admin" / "static" / "data_sources_icons"
MANIFEST = ROOT / "data_plane" / "data_edge_agent" / "admin" / "static" / "type_icons.json"

# --- mirror DataSourceIcon.vue ---------------------------------------------
_ALIASES = {
    "postgres": "postgresql", "sqlserver": "mssql", "sql_server": "mssql",
    "awsathena": "aws_athena", "athena": "aws_athena", "redshift": "aws_redshift",
    "fabric": "ms_fabric", "microsoft_fabric": "ms_fabric",
    "qlik_sense": "qlik", "qlik_sense_onprem": "qlik",
    "hana": "sap_hana", "saphana": "sap_hana", "datasphere": "sap_datasphere",
    "businessobjects": "sap_datasphere", "business_objects": "sap_datasphere",
    "bobj": "sap_datasphere", "sap_bo": "sap_datasphere",
    "sap_bw": "sap_datasphere", "bw": "sap_datasphere",
    "sap_bw_xmla": "sap_datasphere", "bw4hana": "sap_datasphere",
}
_TYPE_ICON_FILE = {
    "csv": "csv.png", "gmail_mail": "gmail.png", "outlook_mail": "outlook_mail.svg",
    "elasticsearch": "elasticsearch.svg", "s3": "s3.svg", "monday": "monday.svg",
}
_TOOL_ICON_TYPES = {"dbt", "lookml", "markdown", "resource", "tableau", "dataform", "mcp", "custom_api"}


def normalize_type(raw: str) -> str:
    t = re.sub(r"\s+", "_", (raw or "").lower().strip())
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    t = re.sub(r"_\d+$", "", t)
    return _ALIASES.get(t, t)


def resolve_asset(type_name: str) -> tuple[str, str]:
    """(subdir, filename) under frontend/public, mirroring the Vue resolver."""
    t = normalize_type(type_name)
    if t in _TYPE_ICON_FILE:
        return "data_sources_icons", _TYPE_ICON_FILE[t]
    if t in _TOOL_ICON_TYPES:
        return "icons", f"{t}.png"
    return "data_sources_icons", f"{t}.png"


def main() -> None:
    types = list(json.loads(SPECS.read_text())["types"].keys())
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    manifest: dict[str, str] = {}
    missing: list[str] = []
    for t in types:
        subdir, filename = resolve_asset(t)
        src = FRONTEND_PUBLIC / subdir / filename
        if not src.is_file():
            missing.append(t)
            continue
        shutil.copy2(src, OUT_DIR / filename)
        manifest[t] = filename

    MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    total = sum(f.stat().st_size for f in OUT_DIR.iterdir())
    print(f"bundled {len(manifest)}/{len(types)} icons ({total // 1024} KiB) -> {OUT_DIR}")
    if missing:
        print(f"no asset for (monogram fallback): {', '.join(sorted(missing))}")


if __name__ == "__main__":
    main()
