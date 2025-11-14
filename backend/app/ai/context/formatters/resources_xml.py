from typing import Any, Dict, List
import json

from app.ai.context.sections.base import xml_tag, xml_escape


def _parse_json_field(field_value: Any):
    if field_value is None:
        return []
    if isinstance(field_value, (list, dict)):
        return field_value
    if isinstance(field_value, str):
        try:
            return json.loads(field_value)
        except Exception:
            return []
    return []


def _format_model(r: Dict[str, Any]) -> str:
    columns_json = _parse_json_field(r.get("columns"))
    columns: List[str] = []
    for col in (columns_json or []):
        lines = [
            xml_tag("name", xml_escape(str(col.get("name", "")))),
            xml_tag("description", xml_escape(str(col.get("description", "")))),
            xml_tag("data_type", xml_escape(str(col.get("data_type", "")))),
        ]
        if col.get("tests") is not None:
            lines.append(xml_tag("tests", xml_escape(str(col.get("tests")))))
        columns.append(xml_tag("column", "\n".join(lines)))

    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "model"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if r.get("sql_content"):
        body.append(xml_tag("sql_content", xml_escape(str(r.get("sql_content")))))
    if columns:
        body.append(xml_tag("columns", "\n".join(columns)))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("model", "\n".join(body))


def _format_metric(r: Dict[str, Any]) -> str:
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    columns_json = _parse_json_field(r.get("columns"))
    columns: List[str] = []
    for col in (columns_json or []):
        columns.append(
            f"    <column name='{xml_escape(str(col.get('name','')))}' description='{xml_escape(str(col.get('description','')))}' data_type='{xml_escape(str(col.get('data_type','')))}' />"
        )
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "metric"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    body.append(xml_tag("label", xml_escape(str(raw_data.get("label", "")))))
    body.append(xml_tag("calculation_method", xml_escape(str(raw_data.get("calculation_method", "")))))
    body.append(xml_tag("expression", xml_escape(str(raw_data.get("expression", "")))))
    if raw_data.get("dimensions"):
        body.append(xml_tag("dimensions", ", ".join(map(str, raw_data.get("dimensions", [])))))
    if raw_data.get("time_grains"):
        body.append(xml_tag("time_grains", ", ".join(map(str, raw_data.get("time_grains", [])))))
    if raw_data.get("filters"):
        filters = [f"{f.get('field','?')}:{f.get('operator','?')}:{f.get('value','?')}" for f in raw_data.get("filters", [])]
        body.append(xml_tag("filters", ", ".join(filters)))
    if columns:
        body.append(xml_tag("columns", "\n".join(columns)))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("metric", "\n".join(body))


def _format_source(r: Dict[str, Any]) -> str:
    columns_json = _parse_json_field(r.get("columns"))
    columns: List[str] = []
    for col in (columns_json or []):
        lines = [
            xml_tag("name", xml_escape(str(col.get("name", "")))),
            xml_tag("description", xml_escape(str(col.get("description", "")))),
            xml_tag("data_type", xml_escape(str(col.get("data_type", "")))),
        ]
        if col.get("tests") is not None:
            lines.append(xml_tag("tests", xml_escape(str(col.get("tests")))))
        columns.append(xml_tag("column", "\n".join(lines)))
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "source"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if r.get("source_name"):
        body.append(xml_tag("source_name", xml_escape(str(r.get("source_name")))))
    if r.get("database"):
        body.append(xml_tag("database", xml_escape(str(r.get("database")))))
    if r.get("schema"):
        body.append(xml_tag("schema", xml_escape(str(r.get("schema")))))
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    if raw_data.get("loader"):
        body.append(xml_tag("loader", xml_escape(str(raw_data.get("loader")))))
    if raw_data.get("freshness"):
        freshness = raw_data.get("freshness", {})
        warn_after = freshness.get("warn_after", {})
        error_after = freshness.get("error_after", {})
        cond = freshness.get("filter")
        txt = []
        if warn_after.get("count"):
            txt.append(f"warn_after: {warn_after.get('count')} {warn_after.get('period')}")
        if error_after.get("count"):
            txt.append(f"error_after: {error_after.get('count')} {error_after.get('period')}")
        if cond:
            txt.append(f"filter: {cond}")
        body.append(xml_tag("freshness", ", ".join(txt)))
    if columns:
        body.append(xml_tag("columns", "\n".join(columns)))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("source", "\n".join(body))


def _format_seed(r: Dict[str, Any]) -> str:
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "seed"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if r.get("database"):
        body.append(xml_tag("database", xml_escape(str(r.get("database")))))
    if r.get("schema"):
        body.append(xml_tag("schema", xml_escape(str(r.get("schema")))))
    columns_json = _parse_json_field(r.get("columns"))
    if columns_json:
        columns = [
            f"    <column name='{xml_escape(str(c.get('name','')))}' data_type='{xml_escape(str(c.get('data_type','')))}' />"
            for c in columns_json
        ]
        body.append(xml_tag("columns", "\n".join(columns)))
    return xml_tag("seed", "\n".join(body))


def _format_macro(r: Dict[str, Any]) -> str:
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "macro"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if r.get("sql_content"):
        body.append(xml_tag("sql_content", xml_escape(str(r.get("sql_content")))))
    if raw_data.get("arguments"):
        args = [f"{a.get('name')}({a.get('type','?')})" for a in raw_data.get("arguments", [])]
        body.append(xml_tag("arguments", ", ".join(args)))
    return xml_tag("macro", "\n".join(body))


def _format_test(r: Dict[str, Any]) -> str:
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "test"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if r.get("sql_content"):
        body.append(xml_tag("sql_content", xml_escape(str(r.get("sql_content")))))
    meta = raw_data.get("test_metadata", {}) if isinstance(raw_data, dict) else {}
    if meta:
        body.append(xml_tag("test_type", xml_escape(str(meta.get("name", "unknown")))))
        if meta.get("kwargs") is not None:
            body.append(xml_tag("test_config", xml_escape(str(meta.get("kwargs")))))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("test", "\n".join(body))


def _format_exposure(r: Dict[str, Any]) -> str:
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "exposure"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    body.append(xml_tag("type", xml_escape(str(raw_data.get("type", "")))))
    body.append(xml_tag("maturity", xml_escape(str(raw_data.get("maturity", "")))))
    if raw_data.get("url"):
        body.append(xml_tag("url", xml_escape(str(raw_data.get("url")))))
    owner = raw_data.get("owner", {}) if isinstance(raw_data, dict) else {}
    if owner:
        body.append(xml_tag("owner_name", xml_escape(str(owner.get("name", "")))))
        body.append(xml_tag("owner_email", xml_escape(str(owner.get("email", "")))))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("exposure", "\n".join(body))


def _format_dataform_table(r: Dict[str, Any]) -> str:
    """
    Format a Dataform table (parsed from .sqlx) into a rich XML-ish representation.

    Expected fields (from MetadataResource + SQLXResourceExtractor):
      - name, resource_type, description, path
      - sql_content (main SELECT body)
      - columns: list[{name, description, data_type, meta}]
      - depends_on: list[str]
      - raw_data: {
          materialization, tags, schema_expr, unique_key,
          partition_by, cluster_by, assertions, sql_body,
          pre_operations_raw, raw_config, ...
        }
    """
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    columns_json = _parse_json_field(r.get("columns"))

    # Columns
    columns: List[str] = []
    for col in (columns_json or []):
        lines = [
            xml_tag("name", xml_escape(str(col.get("name", "")))),
            xml_tag("description", xml_escape(str(col.get("description", "")))),
        ]
        if col.get("data_type") is not None:
            lines.append(xml_tag("data_type", xml_escape(str(col.get("data_type", "")))))
        columns.append(xml_tag("column", "\n".join(lines)))

    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "dataform_table"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))

    # Config / metadata from raw_data
    if raw_data:
        materialization = raw_data.get("materialization", "")
        if materialization:
            body.append(xml_tag("materialization", xml_escape(str(materialization))))

        tags = raw_data.get("tags", [])
        if tags:
            body.append(xml_tag("tags", ", ".join(map(str, tags))))

        schema_expr = raw_data.get("schema_expr", "")
        if schema_expr:
            body.append(xml_tag("schema_expr", xml_escape(str(schema_expr))))

        unique_key = raw_data.get("unique_key", [])
        if unique_key:
            body.append(xml_tag("unique_key", ", ".join(map(str, unique_key))))

        partition_by = raw_data.get("partition_by", "")
        if partition_by:
            body.append(xml_tag("partition_by", xml_escape(str(partition_by))))

        cluster_by = raw_data.get("cluster_by", [])
        if cluster_by:
            body.append(xml_tag("cluster_by", ", ".join(map(str, cluster_by))))

        # Assertions as a raw text blob if present
        assertions = raw_data.get("assertions", {})
        if assertions:
            # Common pattern: {"__raw_text__": "..."}; fall back to full JSON
            raw_text = assertions.get("__raw_text__") if isinstance(assertions, dict) else None
            body.append(
                xml_tag(
                    "assertions",
                    xml_escape(str(raw_text if raw_text is not None else assertions)),
                )
            )

    # SQL body and pre-operations (prefer raw_data.sql_body; fallback to sql_content)
    sql_body = raw_data.get("sql_body") or r.get("sql_content")
    if sql_body:
        body.append(xml_tag("sql_body", xml_escape(str(sql_body))))

    pre_ops = raw_data.get("pre_operations_raw")
    if pre_ops:
        body.append(xml_tag("pre_operations", xml_escape(str(pre_ops))))

    # Columns & dependencies
    if columns:
        body.append(xml_tag("columns", "\n".join(columns)))

    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))

    return xml_tag("dataform_table", "\n".join(body))


def _format_lookml_model(r: Dict[str, Any]) -> str:
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "lookml_model"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if raw_data.get("label"):
        body.append(xml_tag("label", xml_escape(str(raw_data.get("label")))))
    if raw_data.get("connection"):
        body.append(xml_tag("connection", xml_escape(str(raw_data.get("connection")))))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("lookml_model", "\n".join(body))


def _format_lookml_view(r: Dict[str, Any]) -> str:
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    fields_json = _parse_json_field(r.get("columns"))
    fields: List[str] = []
    for fld in (fields_json or []):
        fld_type = str(fld.get("resource_type", "lookml_field")).replace("lookml_", "")
        parts = [
            xml_tag("name", xml_escape(str(fld.get("field_name", fld.get("name", ""))))),
            xml_tag("type", xml_escape(str(fld.get("type", "")))),
            xml_tag("description", xml_escape(str(fld.get("description", "")))),
        ]
        if fld.get("sql"):
            parts.append(xml_tag("sql", xml_escape(str(fld.get("sql")))))
        if fld.get("hidden") is not None:
            parts.append(xml_tag("hidden", xml_escape(str(fld.get("hidden")))))
        if fld.get("tags"):
            parts.append(xml_tag("tags", ", ".join(map(str, fld.get("tags", [])))))
        if fld.get("primary_key") is not None:
            parts.append(xml_tag("primary_key", xml_escape(str(fld.get("primary_key")))))
        fields.append(xml_tag(fld_type, "\n".join(parts)))
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "lookml_view"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if raw_data.get("label"):
        body.append(xml_tag("label", xml_escape(str(raw_data.get("label")))))
    if raw_data.get("sql_table_name"):
        body.append(xml_tag("sql_table_name", xml_escape(str(raw_data.get("sql_table_name")))))
    if raw_data.get("derived_table"):
        body.append(xml_tag("derived_table_sql", xml_escape(str(raw_data.get("derived_table", {}).get("sql", "")))))
    if fields:
        body.append(xml_tag("fields", "\n".join(fields)))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("lookml_view", "\n".join(body))


def _format_lookml_explore(r: Dict[str, Any]) -> str:
    raw_data = _parse_json_field(r.get("raw_data")) if r.get("raw_data") is not None else {}
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "lookml_explore"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if raw_data.get("model_name"):
        body.append(xml_tag("model_name", xml_escape(str(raw_data.get("model_name")))))
    if raw_data.get("label"):
        body.append(xml_tag("label", xml_escape(str(raw_data.get("label")))))
    if raw_data.get("view_name"):
        body.append(xml_tag("view_name", xml_escape(str(raw_data.get("view_name")))))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("lookml_explore", "\n".join(body))


def _format_markdown(r: Dict[str, Any]) -> str:
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "markdown"))))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if r.get("description"):
        body.append(xml_tag("description", xml_escape(str(r.get("description")))))
    # markdown content could live in sql_content or raw_data.content
    content = r.get("sql_content") or (
        (_parse_json_field(r.get("raw_data")) or {}).get("content") if isinstance(_parse_json_field(r.get("raw_data")), dict) else None
    )
    if content:
        body.append(xml_tag("content", xml_escape(str(content))))
    return xml_tag("markdown", "\n".join(body))


def _format_generic(r: Dict[str, Any]) -> str:
    columns_json = _parse_json_field(r.get("columns"))
    columns: List[str] = []
    for col in (columns_json or []):
        columns.append(
            f"    <column name='{xml_escape(str(col.get('name','')))}' description='{xml_escape(str(col.get('description','')))}' data_type='{xml_escape(str(col.get('data_type','')))}' />"
        )
    body: List[str] = []
    body.append(xml_tag("name", xml_escape(str(r.get("name", "")))))
    body.append(xml_tag("resource_type", xml_escape(str(r.get("resource_type", r.get("type", "resource"))))))
    body.append(xml_tag("description", xml_escape(str(r.get("description", "")))))
    body.append(xml_tag("path", xml_escape(str(r.get("path", "")))))
    if r.get("sql_content"):
        body.append(xml_tag("sql_content", xml_escape(str(r.get("sql_content")))))
    if columns:
        body.append(xml_tag("columns", "\n".join(columns)))
    depends_on = _parse_json_field(r.get("depends_on"))
    if depends_on:
        body.append(xml_tag("depends_on", ", ".join(map(str, depends_on))))
    return xml_tag("resource", "\n".join(body))


def format_resource_dict_xml(resource: Dict[str, Any]) -> str:
    """Render a resource dict to the canonical XML-ish shape used across the app.

    The dict is expected to carry fields commonly found on MetadataResource or
    previously serialized payloads: name, resource_type (or type), description,
    path, sql_content, columns, depends_on, raw_data, database, schema,
    source_name, etc.
    """
    rtype = str(resource.get("resource_type") or resource.get("type") or "").lower()
    if rtype == "model":
        return _format_model(resource)
    if rtype == "metric":
        return _format_metric(resource)
    if rtype == "source":
        return _format_source(resource)
    if rtype == "seed":
        return _format_seed(resource)
    if rtype == "macro":
        return _format_macro(resource)
    if rtype in ("test", "singular_test"):
        return _format_test(resource)
    if rtype == "exposure":
        return _format_exposure(resource)
    if rtype == "dataform_table":
        return _format_dataform_table(resource)
    if rtype == "lookml_model":
        return _format_lookml_model(resource)
    if rtype == "lookml_view":
        return _format_lookml_view(resource)
    if rtype == "lookml_explore":
        return _format_lookml_explore(resource)
    if rtype == "markdown":
        return _format_markdown(resource)
    return _format_generic(resource)


