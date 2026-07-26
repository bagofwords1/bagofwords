"""Shared helpers for turning a raw MCP tool input schema into something an
LLM can actually follow, and for checking arguments against it locally.

Two jobs:

``resolve_refs``
    Inline ``$ref``/``$defs`` indirection. MCP servers built on
    zod-to-json-schema (monday, Linear, Notion, Sentry) routinely emit
    ``{"$ref": "#/$defs/Foo"}``. That is fine for a validator but useless to a
    model reading the schema as text — it has to chase a pointer through a JSON
    blob. Inlining puts the real shape at the point of use.

``render_schema_xml``
    Render a resolved schema as compact XML for the ``<mcp_tools>`` context
    block. Deliberately not raw JSON: the block is rebuilt every turn, so it has
    to stay cheap. Types, requiredness, enums and nested shape survive;
    descriptions are kept only where they carry real information.

``validate_arguments``
    Check arguments against the tool's own schema *before* the network call, and
    return path-qualified errors. A local failure is instant and precise; the
    remote equivalent costs a round trip and usually comes back as a vendor
    string that does not name the offending field.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Guard against pathological / self-referential schemas.
_MAX_DEPTH = 12


def resolve_refs(schema: Any, _defs: Optional[Dict[str, Any]] = None, _depth: int = 0) -> Any:
    """Inline local ``$ref`` pointers against ``$defs`` / ``definitions``.

    Only local pointers (``#/$defs/X``, ``#/definitions/X``) are resolved;
    anything else is left alone. Recursion is depth-capped, and a ref that
    cannot be resolved is left as-is rather than dropped — losing a field
    silently would be worse than showing the model a pointer.
    """
    if _depth > _MAX_DEPTH:
        return schema
    if isinstance(schema, list):
        return [resolve_refs(item, _defs, _depth + 1) for item in schema]
    if not isinstance(schema, dict):
        return schema

    defs = dict(_defs or {})
    for key in ("$defs", "definitions"):
        if isinstance(schema.get(key), dict):
            defs.update(schema[key])

    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = None
        for prefix in ("#/$defs/", "#/definitions/"):
            if ref.startswith(prefix):
                name = ref[len(prefix):]
                break
        target = defs.get(name) if name else None
        if target is None:
            return schema
        merged = resolve_refs(target, defs, _depth + 1)
        # Sibling keywords (description, default) win over the target's.
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        if siblings and isinstance(merged, dict):
            merged = {**merged, **resolve_refs(siblings, defs, _depth + 1)}
        return merged

    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("$defs", "definitions"):
            continue  # inlined above; no longer needed downstream
        out[key] = resolve_refs(value, defs, _depth + 1)
    return out


def _type_of(spec: Dict[str, Any]) -> str:
    t = spec.get("type")
    if isinstance(t, list):
        return "|".join(str(x) for x in t)
    if t:
        return str(t)
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(spec.get(key), list):
            parts = [_type_of(s) for s in spec[key] if isinstance(s, dict)]
            parts = [p for p in parts if p and p != "any"]
            if parts:
                return "|".join(dict.fromkeys(parts))
    return "any"


def render_schema_xml(schema: Any, indent: str = "  ", _depth: int = 0) -> str:
    """Render a resolved schema as nested ``<arg>`` elements.

    Emits name, type, requiredness, enum, and — for the shapes that actually
    trip agents up — nested object properties and array item shape.
    """
    from app.ai.context.sections.base import xml_escape

    if not isinstance(schema, dict) or _depth > _MAX_DEPTH:
        return ""
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(schema.get("required") or [])

    lines: List[str] = []
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        attrs = [f'name="{xml_escape(str(name))}"', f'type="{xml_escape(_type_of(spec))}"']
        if name in required:
            attrs.append('required="true"')
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            rendered = ",".join(str(e) for e in enum[:12])
            attrs.append(f'enum="{xml_escape(rendered)}"')
        pattern = spec.get("pattern")
        if isinstance(pattern, str):
            attrs.append(f'pattern="{xml_escape(pattern)}"')

        desc = spec.get("description")
        desc_txt = xml_escape(str(desc)) if isinstance(desc, str) and desc.strip() else ""

        inner = ""
        if spec.get("type") == "object":
            inner = render_schema_xml(spec, indent, _depth + 1)
        elif spec.get("type") == "array" and isinstance(spec.get("items"), dict):
            items = spec["items"]
            item_inner = render_schema_xml(items, indent, _depth + 1)
            if item_inner:
                inner = f"{indent * (_depth + 1)}<item type=\"object\">\n{item_inner}\n{indent * (_depth + 1)}</item>"
            else:
                attrs.append(f'items="{xml_escape(_type_of(items))}"')

        pad = indent * (_depth + 1)
        open_tag = f"{pad}<arg {' '.join(attrs)}>"
        if inner:
            body = (desc_txt + "\n" if desc_txt else "") + inner
            lines.append(f"{open_tag}\n{body}\n{pad}</arg>")
        elif desc_txt:
            lines.append(f"{open_tag}{desc_txt}</arg>")
        else:
            lines.append(f"{pad}<arg {' '.join(attrs)}/>")
    return "\n".join(lines)


def validate_arguments(
    arguments: Any, input_schema: Any
) -> Tuple[bool, List[str]]:
    """Validate ``arguments`` against a tool's declared input schema.

    Returns ``(ok, errors)`` where each error is path-qualified
    (``columnValues: expected string, got object``). Never raises: an
    unusable schema means "cannot validate", which is reported as OK so a
    malformed schema can't block an otherwise-fine call.
    """
    if not isinstance(input_schema, dict) or not input_schema:
        return True, []
    if not isinstance(arguments, dict):
        return False, ["<root>: arguments must be an object"]

    try:
        from jsonschema import Draft202012Validator
    except Exception:
        return True, []

    try:
        validator = Draft202012Validator(input_schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
    except Exception:
        # A schema the validator itself rejects — do not block the call.
        return True, []

    out: List[str] = []
    for err in errors[:12]:
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        msg = err.message
        # jsonschema's phrasing is verbose; the type mismatch is the common
        # case and reads better tightened up.
        if err.validator == "type":
            expected = err.validator_value
            expected = "|".join(expected) if isinstance(expected, list) else expected
            got = type(err.instance).__name__
            got = {
                "dict": "object", "list": "array", "str": "string",
                "int": "integer", "float": "number", "bool": "boolean",
                "NoneType": "null",
            }.get(got, got)
            msg = f"expected {expected}, got {got}"
        out.append(f"{path}: {msg}")
    return (not out), out
