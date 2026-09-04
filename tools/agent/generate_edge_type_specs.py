"""Generate the edge agent's data-source type-spec bundle.

The edge agent's admin UI renders a type-specific Add Connection form. The field
definitions are the backend's own Pydantic config/credentials classes
(`app/schemas/data_sources/configs.py`) surfaced as JSON Schema — the exact same
source the Bow app's ConnectForm.vue renders from. But the registry that maps a
type to those classes imports `app.settings`, which the edge agent deliberately
does not carry. So we snapshot the resolved specs to a self-contained JSON bundle
here (run in the backend venv) and ship that with the agent.

    python tools/agent/generate_edge_type_specs.py \
        data_plane/data_edge_agent/data_sources/type_specs.json

Re-run after changing configs.py or the registry. Excludes the `custom` category
(MCP / Custom API use a different connection flow the tunnel does not serve yet).
"""
import json
import sys

from app.schemas.data_source_registry import REGISTRY

EXCLUDE_CATEGORIES = {"custom"}


def _auth_meta(auth):
    return {
        "default": auth.default,
        "by_auth": {
            mode: {"title": v.title, "scopes": list(getattr(v, "scopes", []) or [])}
            for mode, v in auth.by_auth.items()
        },
    }


def main(out_path):
    types = {}
    for t, e in REGISTRY.items():
        if getattr(e, "category", "databases") in EXCLUDE_CATEGORIES:
            continue
        if getattr(e, "deprecated", False):
            continue
        auth = e.credentials_auth
        types[t] = {
            "type": e.type,
            "title": e.title,
            "description": getattr(e, "description", "") or "",
            "category": getattr(e, "category", "databases"),
            "data_shape": getattr(e, "data_shape", "tables"),
            "ui_form": getattr(e, "ui_form", "data_source"),
            "config": e.config_schema.model_json_schema(),
            "auth": _auth_meta(auth),
            "credentials_by_auth": {
                mode: v.schema.model_json_schema() for mode, v in auth.by_auth.items()
            },
        }
    with open(out_path, "w") as fh:
        json.dump({"version": 1, "types": types}, fh, indent=1, sort_keys=True)
    print(f"wrote {len(types)} types -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "type_specs.json")
