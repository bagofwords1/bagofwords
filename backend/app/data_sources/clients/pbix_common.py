"""Shared helpers for reading Power BI (.pbix) semantic models with pbixray.

Used by both the Power BI Report Server connector (which downloads .pbix blobs
over REST) and the file-based PBIX connector (which reads them from disk).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Auto-generated internal Power BI date tables — filtered out of schema output.
AUTO_DATE_TABLE_RE = re.compile(r"^(LocalDateTable|DateTableTemplate)_[0-9a-fA-F\-]+$")

# DuckDB view names need to be simple identifiers. Vertipaq table names can
# contain spaces, symbols, or be reserved keywords — sanitize to a safe form.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")

# Max pbix size we'll attempt to parse (bytes). Larger files use more
# memory/time than is worthwhile; decoding is fully in-process.
PBIX_MAX_BYTES = 200 * 1024 * 1024  # 200MB

# Per-table row cap when materializing pbix data to Parquet. Tables exceeding
# this are skipped — parsing very large Vertipaq tables blows memory on the
# decode path. 5M is comfortably above typical semantic-model fact tables.
PBIX_MAX_ROWS_PER_TABLE = 5_000_000


def safe_view_name(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("_")
    if not cleaned:
        cleaned = "tbl"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned


def dax_to_dtype(dax_type: str | None) -> str:
    if not dax_type:
        return "unknown"
    t = str(dax_type).lower()
    if "int" in t:
        return "int"
    if "float" in t or "double" in t or "decimal" in t or "number" in t:
        return "float"
    if "bool" in t:
        return "bool"
    if "date" in t or "time" in t:
        return "datetime"
    if "string" in t or "text" in t or "object" in t:
        return "string"
    return t


def extract_pbix_model(pbix_path: str) -> dict[str, Any]:
    """Parse a .pbix file's Vertipaq model with pbixray (metadata only — no
    table data is decoded). Returns {tables, relationships}, where each table
    carries columns and measures. Auto-generated date tables are dropped.

    Raises RuntimeError when pbixray is unavailable or the file can't be parsed.
    """
    try:
        from pbixray import PBIXRay  # type: ignore
    except Exception as e:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "pbixray is required to read Power BI (.pbix) files but is not installed."
        ) from e

    try:
        model = PBIXRay(pbix_path)
        schema_df = model.schema
        rels_df = model.relationships
        measures_df = model.dax_measures
    except Exception as e:
        raise RuntimeError(f"Failed to parse PBIX model from {pbix_path}: {e}") from e

    tables_out: dict[str, dict[str, Any]] = {}
    if schema_df is not None and not schema_df.empty:
        for _, row in schema_df.iterrows():
            tname = str(row.get("TableName") or "")
            if not tname or AUTO_DATE_TABLE_RE.match(tname):
                continue
            t = tables_out.setdefault(tname, {"name": tname, "columns": [], "measures": []})
            t["columns"].append({
                "name": str(row.get("ColumnName") or ""),
                "dtype": dax_to_dtype(row.get("PandasDataType")),
            })

    if measures_df is not None and not measures_df.empty:
        for _, row in measures_df.iterrows():
            tname = str(row.get("TableName") or "")
            if not tname or AUTO_DATE_TABLE_RE.match(tname):
                continue
            entry = tables_out.setdefault(tname, {"name": tname, "columns": [], "measures": []})
            entry["measures"].append({
                "name": str(row.get("Name") or ""),
                "expression": str(row.get("Expression") or ""),
                "display_folder": str(row.get("DisplayFolder") or ""),
                "description": str(row.get("Description") or ""),
            })

    relationships_out: list[dict[str, Any]] = []
    if rels_df is not None and not rels_df.empty:
        for _, row in rels_df.iterrows():
            from_t = str(row.get("FromTableName") or "")
            to_t = str(row.get("ToTableName") or "")
            if AUTO_DATE_TABLE_RE.match(from_t) or AUTO_DATE_TABLE_RE.match(to_t):
                continue
            relationships_out.append({
                "from_table": from_t,
                "from_column": str(row.get("FromColumnName") or ""),
                "to_table": to_t,
                "to_column": str(row.get("ToColumnName") or ""),
                "is_active": bool(row.get("IsActive", True)),
                "cardinality": str(row.get("Cardinality") or ""),
            })

    return {"tables": list(tables_out.values()), "relationships": relationships_out}


def materialize_pbix_parquets(
    pbix_path: str,
    cache_dir: Path,
    *,
    max_rows_per_table: int = PBIX_MAX_ROWS_PER_TABLE,
) -> dict[str, str]:
    """Decode every non-date model table in a .pbix and write one Parquet file
    per table into `cache_dir`. Returns the manifest {model_table: filename}
    and writes it to `cache_dir/manifest.json`.

    Individual table failures (pbixray decode errors, row-cap skips) are logged
    and skipped so one bad table doesn't lose the rest of the model. Raises
    RuntimeError when the model yields no materializable tables at all.
    """
    try:
        from pbixray import PBIXRay  # type: ignore
    except Exception as e:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "pbixray is required to query Power BI (.pbix) files but is not installed."
        ) from e

    model = PBIXRay(pbix_path)
    schema_df = model.schema
    if schema_df is None or schema_df.empty:
        raise RuntimeError(f"PBIX '{pbix_path}' has no tables in its semantic model.")

    candidate_tables: list[str] = []
    seen = set()
    for _, row in schema_df.iterrows():
        tname = str(row.get("TableName") or "")
        if not tname or tname in seen or AUTO_DATE_TABLE_RE.match(tname):
            continue
        seen.add(tname)
        candidate_tables.append(tname)

    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    used_filenames: set = set()

    for tname in candidate_tables:
        try:
            df = model.get_table(tname)
        except Exception as e:
            logger.warning(f"pbix {pbix_path} table '{tname}' extract failed: {e}")
            continue
        if df is None:
            continue
        if len(df) > max_rows_per_table:
            logger.warning(
                f"pbix {pbix_path} table '{tname}' has {len(df)} rows "
                f"(> {max_rows_per_table}); skipping materialization."
            )
            continue

        base = safe_view_name(tname).lower()
        fname = f"{base}.parquet"
        i = 1
        while fname in used_filenames:
            i += 1
            fname = f"{base}_{i}.parquet"
        used_filenames.add(fname)

        fpath = cache_dir / fname
        try:
            df.to_parquet(fpath, index=False)
        except Exception as e:
            logger.warning(f"pbix {pbix_path} table '{tname}' parquet write failed: {e}")
            continue
        manifest[tname] = fname

    if not manifest:
        raise RuntimeError(
            f"PBIX '{pbix_path}' produced no materializable tables (all skipped or failed)."
        )

    with (cache_dir / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f)

    return manifest
