"""Bundle the data-source brand icons the admin UI needs.

The standalone admin SPA runs on the customer's host and can only serve what
ships inside the agent, so the icons for the catalog's types are copied from
the Bow frontend's assets into the agent's static dir, with a `{type: filename}`
manifest the SPA renders from. The copies are a build artifact, not source:
they are gitignored, produced by the Docker build (see data_plane/Dockerfile)
and, in a dev checkout, by the admin server on first start when the bundled
dir is absent. Keeping them out of git is what stops them drifting from the
frontend set.

The type→file resolution mirrors frontend/components/DataSourceIcon.vue
(normalizeType + the explicit overrides) so the icons match what Bow shows.
Types with no matching asset fall back to the monogram tile in the SPA.

Standalone-safe: no package imports, so the Docker builder stage can run it as
a plain script before the package exists in the image:

    python icons.py --frontend-public <dir> --specs <type_specs.json> \\
                    --out <icons dir> --manifest <type_icons.json>
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

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


def bundle(frontend_public: Path, specs: Path, out_dir: Path, manifest_path: Path) -> dict[str, str]:
    """Copy the icons for every type in `specs` and write the manifest.

    Rebuilds `out_dir` from scratch so a removed type's icon does not linger.
    Returns the manifest ({type: filename}); types without an asset are left
    out of it, which the SPA reads as "use the monogram".
    """
    types = list(json.loads(specs.read_text())["types"].keys())
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    manifest: dict[str, str] = {}
    for t in types:
        subdir, filename = resolve_asset(t)
        src = frontend_public / subdir / filename
        if not src.is_file():
            continue
        shutil.copy2(src, out_dir / filename)
        manifest[t] = filename

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return manifest


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--frontend-public", required=True, type=Path)
    ap.add_argument("--specs", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    args = ap.parse_args(argv)

    types = list(json.loads(args.specs.read_text())["types"].keys())
    manifest = bundle(args.frontend_public, args.specs, args.out, args.manifest)
    total = sum(f.stat().st_size for f in args.out.iterdir())
    print(f"bundled {len(manifest)}/{len(types)} icons ({total // 1024} KiB) -> {args.out}")
    missing = sorted(set(types) - set(manifest))
    if missing:
        print(f"no asset for (monogram fallback): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
