"""Compile a list's fields into the ``submit_<slug>`` tool input schema.

The output stays inside the strict JSON-Schema subset both OpenAI and
Anthropic accept for constrained decoding:

- every object has ``additionalProperties: false`` and lists every property
  in ``required`` (optional values are expressed as ``[T, "null"]``), except
  a record's ``fields`` object: an update by ``row_id`` sends only the fields
  it changes, so no field key is required there. "Required for a new row" is
  a row rule, checked in ``records.apply_submission``;
- no numeric/length/pattern keywords — richer checks run server-side in
  ``records.validate_record``;
- construction is deterministic, so the same list always serializes to the
  same bytes and the provider prompt cache (tools are the first cached block)
  stays warm across turns.
"""
from typing import Any, Dict, List

STATUS_VALUES = ["found", "not_found", "ambiguous", "inferred"]
EVIDENCE_KINDS = ["file", "query", "web", "other"]

_TYPE_HINT = {
    "string": "text",
    "number": "number",
    "integer": "whole number",
    "boolean": "true/false",
    "date": "date as YYYY-MM-DD",
    "enum": "one of the allowed values",
}


def _value_schema(field: Dict[str, Any]) -> Dict[str, Any]:
    t = field.get("type") or "string"
    if t == "enum":
        return {"type": ["string", "null"], "enum": list(field.get("enum") or []) + [None]}
    if t == "date":
        return {"type": ["string", "null"], "format": "date"}
    if t in ("number", "integer", "boolean"):
        return {"type": [t, "null"]}
    return {"type": ["string", "null"]}


def _evidence_schema() -> Dict[str, Any]:
    return {
        "type": "array",
        "description": (
            "Where the value came from. For values read from a document, copy a SHORT exact quote "
            "(verbatim, as it appears) and the page number; ref is the file id or name."
        ),
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "ref", "page", "quote"],
            "properties": {
                "kind": {"type": "string", "enum": list(EVIDENCE_KINDS)},
                "ref": {"type": ["string", "null"]},
                "page": {"type": ["integer", "null"]},
                "quote": {"type": ["string", "null"]},
            },
        },
    }


def _field_description(field: Dict[str, Any]) -> str:
    parts: List[str] = []
    desc = (field.get("description") or "").strip()
    if desc:
        parts.append(desc)
    hint = _TYPE_HINT.get(field.get("type") or "string", "text")
    if field.get("unit"):
        hint += f" ({field['unit']})"
    parts.append(f"Value: {hint}.")
    if field.get("type") == "enum":
        parts.append("Allowed: " + ", ".join(field.get("enum") or []) + ".")
    parts.append("Required." if field.get("required") else "Optional.")
    return " ".join(parts)


def _envelope_schema(field: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "anyOf": [
            {
                "type": "object",
                "description": _field_description(field),
                "additionalProperties": False,
                "required": ["value", "status", "evidence", "note"],
                "properties": {
                    "value": _value_schema(field),
                    "status": {
                        "type": "string",
                        "enum": list(STATUS_VALUES),
                        "description": "found = stated in the source; not_found = absent (value null, never guess); "
                                       "ambiguous = several candidates; inferred = derived by reasoning.",
                    },
                    "evidence": _evidence_schema(),
                    "note": {"type": ["string", "null"], "description": "Short justification, esp. for inferred/ambiguous."},
                },
            },
            {"type": "null"},
        ]
    }


def compile_list_schema(fields: List[Dict[str, Any]], description: str = "") -> Dict[str, Any]:
    field_props = {f["name"]: _envelope_schema(f) for f in fields}
    record = {
        "type": "object",
        "additionalProperties": False,
        "required": ["row_id", "fields"],
        "properties": {
            "row_id": {
                "type": ["string", "null"],
                "description": "Existing row id to update (from the list table's _row_id), else null.",
            },
            "fields": {
                "type": "object",
                "additionalProperties": False,
                "properties": field_props,
            },
        },
    }
    schema: Dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["records"],
        "properties": {
            "records": {
                "type": "array",
                "minItems": 1,
                "description": "One record per subject (e.g. one per document/entity).",
                "items": record,
            }
        },
    }
    if description:
        schema["description"] = description.strip()
    return schema


def tool_description(list_name: str, list_description: str, agent_name: str, key_field: str | None,
                     list_id: str | None = None, table_name: str | None = None) -> str:
    lines = [
        f"Save structured records to the list \"{list_name}\" (agent: {agent_name}). "
        + (list_description.strip() + " " if list_description else "")
        + "Gather the facts first with your other tools, then call this once with all records "
        "(one record per subject). Rows are stored durably and shown to the user as a table.",
        "Shape: each record is {row_id, fields}; put the values under \"fields\", "
        "one {value, status, evidence, note} object per field name.",
        "Rules: use status not_found with value null when a value is absent — never guess; "
        "for values read from documents include evidence with an exact short quote and the page.",
        "Updating an existing row: send only the fields you are changing — omitted fields, and fields "
        "set to null (the whole field object), keep their current value. "
        "A new row needs every required field.",
    ]
    if key_field:
        lines.append(f"Records with the same \"{key_field}\" update the existing row instead of adding a new one.")
    lines.append("To update a specific existing row pass its row_id; otherwise row_id is null.")
    if list_id:
        # The id is the only handle the read path accepts; without it here the
        # model guesses (slug, tool-name suffix) and cannot resume a batch.
        lines.append(
            f"This list's id is {list_id}. To read rows back (resume a batch, dedupe, check what is "
            "already saved) call create_data with tables_by_source=[{data_source_id:'builtin:bow', tables:['"
            + (table_name or "bow.<agent>.lists.<list>") + "']}] and in "
            f"code use ds_clients[\"bow\"].execute_query({{\"dataset\": \"list\", \"list_id\": \"{list_id}\"}}) "
            "(optional \"columns\", \"query\" \"field:value\", \"limit\"). For batch extraction read it first "
            "and skip keys already saved."
        )
    return "\n".join(lines)
