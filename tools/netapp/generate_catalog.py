"""Freeze ONTAP Swagger into a diagnostic catalog. Input is the 9.14.1 JSON spec.

Usage: python tools/netapp/generate_catalog.py /path/to/swagger.json
No live cluster access; output includes a disposition for every GET operation.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "backend/app/data_sources/clients/netapp/catalog.json"
SOURCE = "https://docs.netapp.com/us-en/ontap-restapi-9141/swagger-ui/index.html"
# These profiles exclude secret-bearing or content-oriented operations, even GET.
EXCLUDED = {
    "/protocols/ndmp/svms/": "/passwords/",
    "/storage/volumes/": "/files/",
    "/storage/file/clone/tokens": "",
    "/storage/snaplock/file": "",
    "/security/key": "",
    "/security/aws-kms": "",
    "/security/azure-key-vaults": "",
    "/security/gcp-kms": "",
    "/security/login/totps": "",
    "/security/authentication/cluster/oauth2/clients": "",
    "/security/authentication/duo/profiles": "",
    "/protocols/san/iscsi/credentials": "",
    "/protocols/s3/services/": "/users",
    "/support/configuration-backup/backups": "",
    "/support/coredump/coredumps": "",
    "/cluster/ntp/keys": "",
}
BLOCKED = re.compile(
    r"(^|[._-])(password|passphrase|secret|private_key|access_key|secret_key|auth_key|token|certificate_pem)([._-]|$)",
    re.I,
)
CONTROLS = {
    "fields",
    "max_records",
    "return_timeout",
    "return_records",
    "order_by",
    "ignore_unknown_fields",
    "return_metadata",
    "query",
    "count_only",
}
ACTION_PARAMS = {
    "rediscover_trusts",
    "reset_discovered_servers",
    "force",
    "update",
    "refresh",
    "data.offset",
    "data.size",
}


def clean(value):
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", "", value or "")).strip()[:650]


def generate(spec):
    defs = spec["definitions"]

    def resolve(s):
        if "$ref" in s:
            return defs.get(s["$ref"].rsplit("/", 1)[-1], {})
        return s

    def contains_secret(s, seen=()):
        ref = s.get("$ref")
        if ref in seen:
            return False
        if ref:
            seen = seen + (ref,)
        s = resolve(s)
        return any(
            BLOCKED.search(k) or contains_secret(v, seen)
            for k, v in s.get("properties", {}).items()
        ) or ("items" in s and contains_secret(s["items"], seen))

    def fields(s, prefix="", seen=()):
        ref = s.get("$ref")
        if ref and ref in seen:
            return {}
        seen = seen + (ref,) if ref else seen
        s = resolve(s)
        result = {}
        for part in s.get("allOf", []):
            result.update(fields(part, prefix, seen))
        for k, v in s.get("properties", {}).items():
            name = prefix + k
            if k in {"_links", "error", "errors"} or BLOCKED.search(name):
                continue
            v = resolve(v)
            if v.get("type") == "array" and contains_secret(v):
                continue
            if v.get("readOnly") is False and BLOCKED.search(name):
                continue
            if v.get("properties") or v.get("allOf"):
                result.update(fields(v, name + ".", seen))
            else:
                result[name] = {
                    "type": v.get("type", "object"),
                    "format": v.get("format"),
                    "description": clean(v.get("description")),
                }
        return result

    # Explicit public names are stable even when design prose changes.
    aliases = {
        "/storage/aggregates/{uuid}/metrics": "aggregate_metrics",
        "/storage/aggregates": "aggregates",
        "/cluster": "cluster",
        "/cluster/metrics": "cluster_metrics",
        "/storage/cluster": "cluster_space",
        "/storage/disks": "disks",
        "/support/ems/events": "ems_events",
        "/network/ethernet/ports/{uuid}/metrics": "ethernet_port_metrics",
        "/network/ethernet/ports": "ethernet_ports",
        "/network/fc/interfaces/{fc_interface.uuid}/metrics": "fc_interface_metrics",
        "/network/fc/interfaces": "fc_interfaces",
        "/network/fc/logins": "fc_logins",
        "/network/fc/ports/{fc_port.uuid}/metrics": "fc_port_metrics",
        "/network/fc/ports": "fc_ports",
        "/protocols/san/igroups/{igroup.uuid}/igroups": "igroup_children",
        "/protocols/san/igroups/{igroup.uuid}/initiators": "igroup_initiators",
        "/protocols/san/igroups": "igroups",
        "/protocols/san/initiators": "initiators",
        "/network/ip/interfaces/{uuid}/metrics": "ip_interface_metrics",
        "/network/ip/interfaces": "ip_interfaces",
        "/protocols/san/lun-maps": "lun_maps",
        "/storage/luns/{lun.uuid}/metrics": "lun_metrics",
        "/storage/luns": "luns",
        "/cluster/nodes/{uuid}/metrics": "node_metrics",
        "/cluster/nodes": "nodes",
        "/storage/qos/policies": "qos_policies",
        "/storage/shelves": "shelves",
        "/snapmirror/policies": "snapmirror_policies",
        "/snapmirror/relationships": "snapmirror_relationships",
        "/snapmirror/relationships/{relationship.uuid}/transfers": "snapmirror_transfers",
        "/storage/snapshot-policies": "snapshot_policies",
        "/storage/volumes/{volume.uuid}/snapshots": "snapshots",
        "/svm/svms": "svms",
        "/storage/volumes/{volume.uuid}/metrics": "volume_metrics",
        "/storage/volumes": "volumes",
    }
    resources = {}
    coverage = []
    for path, item in sorted(spec["paths"].items()):
        if "get" not in item:
            continue
        op = item["get"]
        reason = None
        if any(path.startswith(a) and b in path for a, b in EXCLUDED.items()):
            reason = "Secret-bearing or content-oriented operation; excluded from diagnostic reads"
        if "/private/" in path:
            reason = "Private API requires a separately reviewed adapter"
        response = resolve(op.get("responses", {}).get("200", {}).get("schema", {}))
        record = response.get("properties", {}).get("records")
        schema = record.get("items", {}) if record else response
        cols = fields(schema)
        if not cols and not reason:
            reason = "No structured 200 response schema; specialist adapter required"
        name = aliases.get(path) or "diag_" + re.sub("[^a-zA-Z0-9]+", "_", path).strip(
            "_"
        )
        if name in resources:
            raise ValueError("Table name collision: " + name)
        params = {}
        parents = []
        for param in op.get("parameters", []):
            if "$ref" in param:
                param = spec.get("parameters", {}).get(param["$ref"].split("/")[-1], {})
            k = param.get("name", "")
            if param.get("in") == "path":
                parents.append(k)
            if (
                param.get("in") != "query"
                or k in CONTROLS
                or k in ACTION_PARAMS
                or BLOCKED.search(k)
                or any(x in k for x in ["reset", "rediscover", "refresh"])
            ):
                continue
            # Filtering is restricted to documented response fields or interval.
            if k in cols or k in {
                "interval",
                "is_constituent",
                "list_destinations_only",
            }:
                params[k] = {
                    "type": param.get("type", "string"),
                    "enum": param.get("enum"),
                }
        coverage.append(
            {
                "path": path,
                "table": name if not reason else None,
                "status": "excluded" if reason else "queryable",
                "reason": reason
                or "Structured diagnostic GET with explicit field/parameter profile",
            }
        )
        if reason:
            continue
        resources[name] = {
            "path": path,
            "collection": bool(record),
            "controls": [
                p.get("name")
                for p in op.get("parameters", [])
                if p.get("name") in CONTROLS
            ],
            "fields": cols,
            "filters": params,
            "parents": parents,
            "description": clean(op.get("summary") or op.get("description")).split(
                "Related ONTAP commands"
            )[0],
            "history": path.endswith("/metrics"),
            "curated": path in aliases,
        }
    return {
        "version": "9.14.1",
        "source": SOURCE,
        "sha256": hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "resources": resources,
        "coverage": coverage,
    }


if __name__ == "__main__":
    result = generate(json.loads(Path(sys.argv[1]).read_text()))
    OUT.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    print(
        f"{len(result['resources'])} diagnostic tables; {len(result['coverage'])} GET dispositions; {OUT.stat().st_size} bytes"
    )
