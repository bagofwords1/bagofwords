"""Kubernetes data source client.

Talks to the Kubernetes API server directly (`/api/v1/...`, `/apis/<group>/...`).
There is no query language; queries are JSON specs (see `system_prompt`) that
map to a catalog of virtual tables, the way the Zabbix and Aria Operations
connectors work.

Three layers of tables live on ONE connection, because an investigation
crosses them (the pod that is CrashLoopBackOff in inventory is the one whose
last log lines and OOM events explain why):

  - FIXED tables declared in code (`_CATALOG`): workloads, nodes, events,
    networking (services, endpoint slices, ingresses, ingress classes,
    network policies) and storage (claims, volumes, storage classes, CSI
    drivers) with curated columns and foreign keys wiring the object graph.
  - DISCOVERED tables, one per *populated* custom resource definition,
    named `crd::<group>/<Kind>` — Argo, cert-manager, Prometheus Operator …
    become queryable with zero vendor-specific code.
  - RUNTIME signals: `pod_metrics` / `node_metrics` from metrics-server
    (emitted only when the cluster serves `metrics.k8s.io`) and `logs`.

Auth is ONE mechanism: the bearer token of a long-lived service-account
token Secret, delivered as an *access file* (a kubeconfig-shaped document
printed by `tools/kubernetes/print_access_file.sh`). `parse_access_file`
accepts only what that script produces — one cluster with `server` +
`certificate-authority-data`, one user with `token` — and rejects exec
credential plugins, client certificates, auth providers and the like by name.

Security posture: the token is cluster-wide read-only, so the CONNECTOR is the
guard against Secrets, not RBAC. There is no `secrets` table, the raw-path
escape hatch refuses `secrets`/`exec`/`attach`/`portforward`/`proxy` paths,
`configmaps` exposes key names only, `raw: true` redacts container env
literals, and the `kubectl.kubernetes.io/last-applied-configuration`
annotation is dropped everywhere (it embeds the full spec, env included).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Tuple

import pandas as pd
import yaml
from kubernetes.client import ApiClient, Configuration
from kubernetes.client.rest import ApiException

from app.ai.prompt_formatters import ForeignKey, ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients.progress import ProgressCallback

logger = logging.getLogger(__name__)


MAX_ROWS = 50_000             # hard cap per execute_query
DEFAULT_LIMIT = 500           # when the query spec omits `limit`
PAGE_SIZE = 500               # API `limit` per list page (server paginates via `continue`)
MAX_PAGES = 200               # safety cap on pagination loops
CRD_PROBE_CAP = 200           # CRDs probed for population during discovery
ANNOTATION_CAP_BYTES = 4096   # per-object cap on the serialized annotations map
ENV_REDACTED = "<redacted>"
CRD_TABLE_PREFIX = "crd::"
METRICS_GROUP_PATH = "/apis/metrics.k8s.io/v1beta1"
LOG_ACCEPT = "text/plain, */*"   # see _query_logs

# Annotations that are dropped unconditionally (raw mode included): they embed
# the full last-applied spec, container env literals included.
_DROPPED_ANNOTATIONS = {"kubectl.kubernetes.io/last-applied-configuration"}

# CRD groups that describe the control plane itself, never worth a table.
_INTERNAL_CRD_GROUPS = ("metrics.k8s.io", "apiregistration.k8s.io", "apiextensions.k8s.io")

# Path segments the escape hatch refuses (case-insensitive): Secrets, and
# subresources that reach INTO a workload rather than describing it.
_FORBIDDEN_SEGMENTS = {"secrets", "exec", "attach", "portforward", "proxy"}

# Every path segment the connector builds or forwards — resource names,
# namespaces, pod names, escape-hatch paths — must be a plain Kubernetes name.
# No `%` (percent-encoding would let `%73ecrets` slip past the segment guard:
# urllib3 sends escapes verbatim, the apiserver routes on the decoded path),
# no `/` inside a name (`default%2Fsecrets`), no `..`, no `?`/`#`. Kubernetes
# names never legitimately contain any of these, so rejecting is cheaper and
# safer than decoding.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Query parameters that turn a request into a long-lived stream. urllib3's
# read timeout only measures gaps between chunks, so a watch/follow on a busy
# resource pins the worker thread and buffers the body for as long as the
# apiserver keeps talking (30–60 min for watches).
_STREAMING_PARAMS = {"watch", "follow", "timeoutseconds", "allowwatchbookmarks", "sendinitialevents"}


def _check_segment(value: Any, field: str) -> str:
    """Validate one path segment (a name/namespace/pod) and return it."""
    if not isinstance(value, str) or value in (".", "..") or not _SEGMENT_RE.match(value):
        raise ValueError(
            f"`{field}` must be a plain Kubernetes name (letters, digits, '.', '_' and '-'; "
            f"no '/', '%', '?', '#' or '..'), got {str(value)[:80]!r}."
        )
    return value

_RELATIVE_RE = re.compile(r"^-?(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

# Kubernetes quantity suffixes → multiplier (decimal SI and binary).
_QUANTITY_SUFFIX = {
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
}
_QUANTITY_RE = re.compile(r"^\s*([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*([a-zA-Z]*)\s*$")

# Volume-source keys a PersistentVolume spec may carry (everything that is not
# one of the generic spec fields is a source).
_PV_NON_SOURCE_KEYS = {
    "accessModes", "capacity", "claimRef", "mountOptions", "nodeAffinity",
    "persistentVolumeReclaimPolicy", "storageClassName", "volumeAttributesClassName",
    "volumeMode",
}


# ── access file ──────────────────────────────────────────────────────────────


def parse_access_file(text: str) -> Tuple[str, Optional[str], str]:
    """Parse the access file printed by `tools/kubernetes/print_access_file.sh`
    into `(api_server_url, ca_pem_or_None, bearer_token)`.

    Strict by design — this is the ONLY credential shape the connector
    accepts. Anything a laptop kubeconfig typically carries (exec plugins,
    client certificates, auth providers, several contexts) is rejected with a
    message naming the offending stanza, so a wrong paste fails immediately
    and explains itself instead of hanging on a missing `aws`/`gcloud` binary.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("The cluster access file is empty. Paste the output of print_access_file.sh.")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"The cluster access file is not valid YAML: {e}")
    if not isinstance(doc, dict):
        raise ValueError("The cluster access file must be a YAML document with `clusters` and `users` sections.")

    clusters = doc.get("clusters")
    users = doc.get("users")
    if not isinstance(clusters, list) or not clusters:
        raise ValueError("The cluster access file has no `clusters` section.")
    if not isinstance(users, list) or not users:
        raise ValueError("The cluster access file has no `users` section.")
    if len(clusters) != 1:
        raise ValueError(
            f"The cluster access file lists {len(clusters)} clusters; exactly one is expected. "
            "Paste the file printed by print_access_file.sh, not a personal kubeconfig."
        )
    if len(users) != 1:
        raise ValueError(
            f"The cluster access file lists {len(users)} users; exactly one is expected. "
            "Paste the file printed by print_access_file.sh, not a personal kubeconfig."
        )
    contexts = doc.get("contexts") or []
    if isinstance(contexts, list) and len(contexts) > 1:
        raise ValueError(
            f"The cluster access file lists {len(contexts)} contexts; at most one is expected."
        )

    cluster = (clusters[0] or {}).get("cluster") or {}
    user = (users[0] or {}).get("user") or {}
    if not isinstance(cluster, dict) or not isinstance(user, dict):
        raise ValueError("The cluster access file's `clusters[0].cluster` and `users[0].user` must be mappings.")

    # ── user stanza ──
    for stanza in ("exec", "auth-provider", "client-certificate", "client-certificate-data",
                   "client-key", "client-key-data", "username", "password", "tokenFile", "token-file"):
        if stanza in user:
            raise ValueError(
                f"The cluster access file's user carries a `{stanza}` stanza, which is not accepted. "
                "Only a service-account `token` is supported — paste the file printed by "
                "print_access_file.sh, not a personal kubeconfig."
            )
    token = user.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("The cluster access file's user has no `token`.")
    token = token.strip()
    if not _JWT_RE.match(token):
        raise ValueError(
            "The cluster access file's `token` is not a service-account token (expected a JWT). "
            "Re-run print_access_file.sh after applying rbac.yaml."
        )
    # ── cluster stanza ──
    server = cluster.get("server")
    if not isinstance(server, str) or not server.strip():
        raise ValueError("The cluster access file's cluster has no `server` URL.")
    server = server.strip().rstrip("/")
    if not re.match(r"^https?://", server):
        raise ValueError(f"The cluster `server` must be an http(s) URL, got '{server[:80]}'.")
    if cluster.get("insecure-skip-tls-verify"):
        raise ValueError(
            "The cluster access file sets `insecure-skip-tls-verify`; that is not accepted. "
            "Re-run print_access_file.sh so the cluster CA is embedded instead."
        )
    if cluster.get("certificate-authority"):
        raise ValueError(
            "The cluster access file references a `certificate-authority` FILE; it must embed the CA "
            "as `certificate-authority-data` (re-run print_access_file.sh)."
        )
    ca_pem: Optional[str] = None
    ca_b64 = cluster.get("certificate-authority-data")
    if ca_b64 is not None:
        if not isinstance(ca_b64, str) or not ca_b64.strip():
            raise ValueError("`certificate-authority-data` is present but empty.")
        try:
            ca_pem = base64.b64decode(ca_b64.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise ValueError("`certificate-authority-data` is not valid base64.")
        if "-----BEGIN CERTIFICATE-----" not in ca_pem:
            raise ValueError("`certificate-authority-data` does not decode to a PEM certificate.")
    elif server.startswith("https://"):
        raise ValueError(
            "The cluster access file has no `certificate-authority-data` for an https server. "
            "Re-run print_access_file.sh so the cluster CA is embedded."
        )

    return server, ca_pem, token


# ── small parsers ─────────────────────────────────────────────────────────────


def parse_quantity(value: Any) -> Optional[float]:
    """Kubernetes quantity → float in base units (`250m` → 0.25, `1Gi` → 2**30,
    `1e3` → 1000). None for empty/unparseable input."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _QUANTITY_RE.match(str(value))
    if not m:
        return None
    number, suffix = m.group(1), m.group(2)
    if suffix not in _QUANTITY_SUFFIX:
        return None
    try:
        return float(number) * _QUANTITY_SUFFIX[suffix]
    except ValueError:
        return None


def cpu_millicores(value: Any) -> Optional[int]:
    q = parse_quantity(value)
    return None if q is None else int(round(q * 1000))


def to_bytes(value: Any) -> Optional[int]:
    q = parse_quantity(value)
    return None if q is None else int(round(q))


def parse_rfc3339(value: Any) -> Optional[pd.Timestamp]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return pd.Timestamp(dt.astimezone(timezone.utc))


def parse_duration_seconds(value: Any) -> Optional[int]:
    """`30s` / `1m0s` / `-1h` / 900 → seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(abs(value))
    s = str(value).strip()
    if s.lstrip("-").isdigit():
        return int(s.lstrip("-"))
    m = _RELATIVE_RE.match(s)
    if m:
        return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
    # Go duration strings like 1h30m10s (what metrics-server's `window` uses).
    total = 0
    matched = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)([smhd])", s):
        total += float(num) * _UNIT_SECONDS[unit]
        matched = True
    return int(total) if matched else None


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"), default=str)


def clean_annotations(annotations: Optional[dict]) -> dict:
    """Drop the last-applied-configuration blob and cap the remaining map."""
    if not isinstance(annotations, dict):
        return {}
    out = {k: v for k, v in annotations.items() if k not in _DROPPED_ANNOTATIONS}
    if len(_json(out).encode("utf-8")) <= ANNOTATION_CAP_BYTES:
        return out
    # Over the cap: truncate values, longest first, until it fits.
    budget = ANNOTATION_CAP_BYTES
    capped: dict = {}
    for k in sorted(out, key=lambda key: len(str(out[key]))):
        v = str(out[k]) if not isinstance(out[k], str) else out[k]
        remaining = budget - len(k) - 8
        if remaining <= 0:
            break
        if len(v) > remaining:
            v = v[: max(remaining - 1, 0)] + "…"
        capped[k] = v
        budget -= len(k) + len(v) + 8
    return capped


def redact_object(obj: dict) -> dict:
    """The `raw` column: the object as stored, minus the two places secrets
    leak — container env literals and the last-applied annotation."""
    out = json.loads(json.dumps(obj, default=str))
    meta = out.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("annotations"), dict):
        meta["annotations"] = {k: v for k, v in meta["annotations"].items() if k not in _DROPPED_ANNOTATIONS}

    def _redact_pod_spec(spec: Any) -> None:
        if not isinstance(spec, dict):
            return
        for key in ("containers", "initContainers", "ephemeralContainers"):
            for c in spec.get(key) or []:
                for env in (c or {}).get("env") or []:
                    if isinstance(env, dict) and "value" in env:
                        env["value"] = ENV_REDACTED

    spec = out.get("spec")
    _redact_pod_spec(spec)                                            # Pod
    if isinstance(spec, dict):
        _redact_pod_spec((spec.get("template") or {}).get("spec"))    # Deployment/RS/STS/DS/Job
        jt = (spec.get("jobTemplate") or {}).get("spec") or {}         # CronJob
        _redact_pod_spec((jt.get("template") or {}).get("spec"))
    return out


# ── row builders ──────────────────────────────────────────────────────────────
#
# Each takes the raw object dict and returns the curated columns. Metadata
# columns (`labels`, `annotations`) are appended uniformly by `_finish_row`.


def _meta(obj: dict) -> dict:
    return obj.get("metadata") or {}


def _owner(obj: dict) -> Tuple[Optional[str], Optional[str]]:
    refs = _meta(obj).get("ownerReferences") or []
    if not refs:
        return None, None
    ctrl = next((r for r in refs if r.get("controller")), refs[0])
    return ctrl.get("kind"), ctrl.get("name")


def _condition(obj: dict, ctype: str) -> Optional[dict]:
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return None


def _row_namespace(o: dict) -> dict:
    return {"name": _meta(o).get("name"), "phase": (o.get("status") or {}).get("phase"),
            "created": parse_rfc3339(_meta(o).get("creationTimestamp"))}


def _row_node(o: dict) -> dict:
    meta, status, spec = _meta(o), o.get("status") or {}, o.get("spec") or {}
    labels = meta.get("labels") or {}
    ready = _condition(o, "Ready")
    roles = sorted(k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/"))
    info = status.get("nodeInfo") or {}
    cap, alloc = status.get("capacity") or {}, status.get("allocatable") or {}
    internal_ip = next((a.get("address") for a in status.get("addresses") or [] if a.get("type") == "InternalIP"), None)
    return {
        "name": meta.get("name"),
        "ready": (ready or {}).get("status") == "True",
        "unschedulable": bool(spec.get("unschedulable")),
        "roles": ",".join(roles),
        "zone": labels.get("topology.kubernetes.io/zone") or labels.get("failure-domain.beta.kubernetes.io/zone"),
        "region": labels.get("topology.kubernetes.io/region") or labels.get("failure-domain.beta.kubernetes.io/region"),
        "instance_type": labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type"),
        "kubelet_version": info.get("kubeletVersion"),
        "os_image": info.get("osImage"),
        "container_runtime": info.get("containerRuntimeVersion"),
        "internal_ip": internal_ip,
        "cpu_capacity_millicores": cpu_millicores(cap.get("cpu")),
        "memory_capacity_bytes": to_bytes(cap.get("memory")),
        "cpu_allocatable_millicores": cpu_millicores(alloc.get("cpu")),
        "memory_allocatable_bytes": to_bytes(alloc.get("memory")),
        "conditions": _json([{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
                             for c in status.get("conditions") or []]),
        "taints": _json(spec.get("taints") or []),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _pod_ready_and_restarts(o: dict) -> Tuple[str, int]:
    statuses = (o.get("status") or {}).get("containerStatuses") or []
    total = len((o.get("spec") or {}).get("containers") or []) or len(statuses)
    ready = sum(1 for s in statuses if s.get("ready"))
    restarts = sum(int(s.get("restartCount") or 0) for s in statuses)
    return f"{ready}/{total}", restarts


def _row_pod(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    labels = meta.get("labels") or {}
    owner_kind, owner_name = _owner(o)
    ready, restarts = _pod_ready_and_restarts(o)
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "app": labels.get("app.kubernetes.io/name") or labels.get("app"),
        "node": spec.get("nodeName"), "phase": status.get("phase"),
        "ready": ready, "restarts": restarts,
        "owner_kind": owner_kind, "owner_name": owner_name,
        "qos_class": status.get("qosClass"), "pod_ip": status.get("podIP"),
        "service_account": spec.get("serviceAccountName"),
        "priority_class": spec.get("priorityClassName"),
        "started_at": parse_rfc3339(status.get("startTime")),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _container_rows(o: dict) -> List[dict]:
    """One row per container of a pod (init containers flagged)."""
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    by_name: Dict[str, dict] = {}
    for s in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
        by_name[s.get("name")] = s
    rows = []
    for init, key in ((False, "containers"), (True, "initContainers")):
        for c in spec.get(key) or []:
            s = by_name.get(c.get("name")) or {}
            state = s.get("state") or {}
            state_name = next(iter(state), None)
            detail = state.get(state_name) or {} if state_name else {}
            last = (s.get("lastState") or {}).get("terminated") or {}
            res = c.get("resources") or {}
            req, lim = res.get("requests") or {}, res.get("limits") or {}
            rows.append({
                "pod": meta.get("name"), "namespace": meta.get("namespace"),
                "name": c.get("name"), "image": c.get("image"), "init": init,
                "ready": bool(s.get("ready")), "state": state_name,
                "reason": detail.get("reason"),
                "exit_code": detail.get("exitCode") if state_name == "terminated" else None,
                "restarts": int(s.get("restartCount") or 0),
                "last_state_reason": last.get("reason"),
                "last_exit_code": last.get("exitCode"),
                "cpu_request_millicores": cpu_millicores(req.get("cpu")),
                "cpu_limit_millicores": cpu_millicores(lim.get("cpu")),
                "memory_request_bytes": to_bytes(req.get("memory")),
                "memory_limit_bytes": to_bytes(lim.get("memory")),
            })
    return rows


def _images(pod_template: dict) -> str:
    spec = (pod_template or {}).get("spec") or {}
    return ",".join(c.get("image") or "" for c in spec.get("containers") or [])


def _conditions_json(o: dict) -> str:
    return _json([{k: c.get(k) for k in ("type", "status", "reason", "message") if c.get(k) is not None}
                  for c in (o.get("status") or {}).get("conditions") or []])


def _row_deployment(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "replicas": spec.get("replicas"), "ready": status.get("readyReplicas") or 0,
        "updated": status.get("updatedReplicas") or 0, "available": status.get("availableReplicas") or 0,
        "unavailable": status.get("unavailableReplicas") or 0,
        "strategy": (spec.get("strategy") or {}).get("type"),
        "selector": _json((spec.get("selector") or {}).get("matchLabels")),
        "images": _images(spec.get("template") or {}),
        "conditions": _conditions_json(o),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_replicaset(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    _, owner_name = _owner(o)
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"), "owner_name": owner_name,
        "replicas": spec.get("replicas"), "ready": status.get("readyReplicas") or 0,
        "available": status.get("availableReplicas") or 0,
        "revision": (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision"),
        "images": _images(spec.get("template") or {}),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_statefulset(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "replicas": spec.get("replicas"), "ready": status.get("readyReplicas") or 0,
        "current": status.get("currentReplicas") or 0, "updated": status.get("updatedReplicas") or 0,
        "service_name": spec.get("serviceName"),
        "update_strategy": (spec.get("updateStrategy") or {}).get("type"),
        "images": _images(spec.get("template") or {}),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_daemonset(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "desired": status.get("desiredNumberScheduled") or 0, "current": status.get("currentNumberScheduled") or 0,
        "ready": status.get("numberReady") or 0, "available": status.get("numberAvailable") or 0,
        "misscheduled": status.get("numberMisscheduled") or 0,
        "images": _images(spec.get("template") or {}),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_job(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    _, owner_name = _owner(o)
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"), "owner_name": owner_name,
        "active": status.get("active") or 0, "succeeded": status.get("succeeded") or 0,
        "failed": status.get("failed") or 0,
        "completions": spec.get("completions"), "parallelism": spec.get("parallelism"),
        "start_time": parse_rfc3339(status.get("startTime")),
        "completion_time": parse_rfc3339(status.get("completionTime")),
        "conditions": _conditions_json(o),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_cronjob(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "schedule": spec.get("schedule"), "suspend": bool(spec.get("suspend")),
        "concurrency_policy": spec.get("concurrencyPolicy"),
        "active_count": len(status.get("active") or []),
        "last_schedule_time": parse_rfc3339(status.get("lastScheduleTime")),
        "last_successful_time": parse_rfc3339(status.get("lastSuccessfulTime")),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_hpa(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    ref = spec.get("scaleTargetRef") or {}
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "target_kind": ref.get("kind"), "target_name": ref.get("name"),
        "min_replicas": spec.get("minReplicas"), "max_replicas": spec.get("maxReplicas"),
        "current_replicas": status.get("currentReplicas"), "desired_replicas": status.get("desiredReplicas"),
        "current_metrics": _json(status.get("currentMetrics") or []),
        "conditions": _conditions_json(o),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_configmap(o: dict) -> dict:
    meta = _meta(o)
    keys = sorted(list((o.get("data") or {}).keys()) + list((o.get("binaryData") or {}).keys()))
    return {"name": meta.get("name"), "namespace": meta.get("namespace"),
            "keys": ",".join(keys), "key_count": len(keys),
            "created": parse_rfc3339(meta.get("creationTimestamp"))}


def _row_resource_quota(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    return {"name": meta.get("name"), "namespace": meta.get("namespace"),
            "hard": _json(status.get("hard") or spec.get("hard")), "used": _json(status.get("used")),
            "scopes": ",".join(spec.get("scopes") or []),
            "created": parse_rfc3339(meta.get("creationTimestamp"))}


def _row_event(o: dict) -> dict:
    meta, inv = _meta(o), o.get("involvedObject") or {}
    first = o.get("firstTimestamp") or o.get("eventTime") or meta.get("creationTimestamp")
    last = o.get("lastTimestamp") or o.get("eventTime") or first
    return {
        "namespace": meta.get("namespace"), "type": o.get("type"), "reason": o.get("reason"),
        "message": o.get("message"),
        "involved_kind": inv.get("kind"), "involved_name": inv.get("name"),
        "involved_namespace": inv.get("namespace"),
        "source_component": (o.get("source") or {}).get("component"),
        "reporting_controller": o.get("reportingComponent") or o.get("reportingController"),
        "count": o.get("count") or 1,
        "first_seen": parse_rfc3339(first), "last_seen": parse_rfc3339(last),
    }


def _row_service(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    lb = [i.get("ip") or i.get("hostname") for i in (status.get("loadBalancer") or {}).get("ingress") or []]
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"), "type": spec.get("type"),
        "cluster_ip": spec.get("clusterIP"), "external_ips": ",".join(spec.get("externalIPs") or []),
        "ports": _json([{k: p.get(k) for k in ("name", "port", "targetPort", "nodePort", "protocol") if p.get(k) is not None}
                        for p in spec.get("ports") or []]),
        "selector": _json(spec.get("selector")),
        "load_balancer_ingress": ",".join(x for x in lb if x),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_endpoint_slice(o: dict) -> dict:
    meta = _meta(o)
    endpoints = o.get("endpoints") or []
    ready = sum(1 for e in endpoints if ((e.get("conditions") or {}).get("ready") is not False))
    addresses = [a for e in endpoints for a in (e.get("addresses") or [])]
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "service": (meta.get("labels") or {}).get("kubernetes.io/service-name"),
        "address_type": o.get("addressType"), "addresses": ",".join(addresses),
        "ready_count": ready, "not_ready_count": len(endpoints) - ready,
        "ports": _json([{k: p.get(k) for k in ("name", "port", "protocol") if p.get(k) is not None}
                        for p in o.get("ports") or []]),
    }


def _row_ingress(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    rules = []
    for r in spec.get("rules") or []:
        for p in ((r.get("http") or {}).get("paths") or []):
            svc = ((p.get("backend") or {}).get("service") or {})
            rules.append({"host": r.get("host"), "path": p.get("path"), "path_type": p.get("pathType"),
                          "service": svc.get("name"),
                          "port": (svc.get("port") or {}).get("number") or (svc.get("port") or {}).get("name")})
    lb = [i.get("ip") or i.get("hostname") for i in (status.get("loadBalancer") or {}).get("ingress") or []]
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "ingress_class": spec.get("ingressClassName") or (meta.get("annotations") or {}).get("kubernetes.io/ingress.class"),
        "hosts": ",".join(r.get("host") or "" for r in spec.get("rules") or []),
        "rules": _json(rules),
        "tls_hosts": ",".join(h for t in spec.get("tls") or [] for h in (t.get("hosts") or [])),
        "load_balancer_ingress": ",".join(x for x in lb if x),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_ingress_class(o: dict) -> dict:
    meta, spec = _meta(o), o.get("spec") or {}
    return {"name": meta.get("name"), "controller": spec.get("controller"),
            "is_default": (meta.get("annotations") or {}).get("ingressclass.kubernetes.io/is-default-class") == "true",
            "parameters": _json(spec.get("parameters")),
            "created": parse_rfc3339(meta.get("creationTimestamp"))}


def _row_network_policy(o: dict) -> dict:
    meta, spec = _meta(o), o.get("spec") or {}
    return {"name": meta.get("name"), "namespace": meta.get("namespace"),
            "pod_selector": _json(spec.get("podSelector")),
            "policy_types": ",".join(spec.get("policyTypes") or []),
            "ingress_rules": _json(spec.get("ingress") or []), "egress_rules": _json(spec.get("egress") or []),
            "created": parse_rfc3339(meta.get("creationTimestamp"))}


def _row_pvc(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"), "phase": status.get("phase"),
        "volume_name": spec.get("volumeName"), "storage_class": spec.get("storageClassName"),
        "access_modes": ",".join(spec.get("accessModes") or []),
        "requested_bytes": to_bytes(((spec.get("resources") or {}).get("requests") or {}).get("storage")),
        "capacity_bytes": to_bytes((status.get("capacity") or {}).get("storage")),
        "volume_mode": spec.get("volumeMode"),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_pv(o: dict) -> dict:
    meta, spec, status = _meta(o), o.get("spec") or {}, o.get("status") or {}
    claim = spec.get("claimRef") or {}
    source_type = next((k for k in spec if k not in _PV_NON_SOURCE_KEYS and isinstance(spec.get(k), dict)), None)
    csi = spec.get("csi") or {}
    return {
        "name": meta.get("name"), "phase": status.get("phase"),
        "capacity_bytes": to_bytes((spec.get("capacity") or {}).get("storage")),
        "access_modes": ",".join(spec.get("accessModes") or []),
        "reclaim_policy": spec.get("persistentVolumeReclaimPolicy"),
        "storage_class": spec.get("storageClassName"), "volume_mode": spec.get("volumeMode"),
        "claim_namespace": claim.get("namespace"), "claim_name": claim.get("name"),
        "source_type": source_type, "csi_driver": csi.get("driver"), "csi_volume_handle": csi.get("volumeHandle"),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_storage_class(o: dict) -> dict:
    meta = _meta(o)
    ann = meta.get("annotations") or {}
    return {
        "name": meta.get("name"), "provisioner": o.get("provisioner"),
        "reclaim_policy": o.get("reclaimPolicy"), "volume_binding_mode": o.get("volumeBindingMode"),
        "allow_volume_expansion": bool(o.get("allowVolumeExpansion")),
        "is_default": (ann.get("storageclass.kubernetes.io/is-default-class")
                       or ann.get("storageclass.beta.kubernetes.io/is-default-class")) == "true",
        "parameters": _json(o.get("parameters")),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_csi_driver(o: dict) -> dict:
    meta, spec = _meta(o), o.get("spec") or {}
    return {
        "name": meta.get("name"), "attach_required": spec.get("attachRequired"),
        "pod_info_on_mount": spec.get("podInfoOnMount"),
        "volume_lifecycle_modes": ",".join(spec.get("volumeLifecycleModes") or []),
        "storage_capacity": spec.get("storageCapacity"), "fs_group_policy": spec.get("fsGroupPolicy"),
        "requires_republish": spec.get("requiresRepublish"),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_crd(o: dict) -> dict:
    meta, spec = _meta(o), o.get("spec") or {}
    names = spec.get("names") or {}
    versions = spec.get("versions") or []
    established = _condition(o, "Established")
    return {
        "name": meta.get("name"), "group": spec.get("group"), "kind": names.get("kind"),
        "plural": names.get("plural"), "scope": spec.get("scope"),
        "served_versions": ",".join(v.get("name") for v in versions if v.get("served")),
        "storage_version": next((v.get("name") for v in versions if v.get("storage")), None),
        "established": (established or {}).get("status") == "True",
        "created": parse_rfc3339(meta.get("creationTimestamp")),
    }


def _row_custom(o: dict) -> dict:
    meta = _meta(o)
    return {
        "name": meta.get("name"), "namespace": meta.get("namespace"),
        "generation": meta.get("generation"),
        "created": parse_rfc3339(meta.get("creationTimestamp")),
        "conditions": _conditions_json(o),
        "spec": _json(o.get("spec")), "status": _json(o.get("status")),
    }


# ── fixed catalog ─────────────────────────────────────────────────────────────
#
# `api`: group path; `plural`: resource; `namespaced`; `columns`: curated
# (name, dtype); `pk`; `fks`: (column, table, ref_column); `row`: builder.
# Object-backed tables get `labels`/`annotations` appended by `_finish_row`
# (`metadata=False` opts out for derived rows).

_TS, _JS = "datetime", "json"

_CATALOG: Dict[str, dict] = {
    # ── core / workloads ──
    "namespaces": dict(
        api="/api/v1", plural="namespaces", namespaced=False, pk="name", row=_row_namespace,
        columns=[("name", "str"), ("phase", "str"), ("created", _TS)], fks=[],
        desc="Namespaces. phase Active|Terminating.",
    ),
    "nodes": dict(
        api="/api/v1", plural="nodes", namespaced=False, pk="name", row=_row_node,
        columns=[("name", "str"), ("ready", "bool"), ("unschedulable", "bool"), ("roles", "str"),
                 ("zone", "str"), ("region", "str"), ("instance_type", "str"),
                 ("kubelet_version", "str"), ("os_image", "str"), ("container_runtime", "str"),
                 ("internal_ip", "str"), ("cpu_capacity_millicores", "int"), ("memory_capacity_bytes", "int"),
                 ("cpu_allocatable_millicores", "int"), ("memory_allocatable_bytes", "int"),
                 ("conditions", _JS), ("taints", _JS), ("created", _TS)],
        fks=[],
        desc=("Cluster nodes with capacity/allocatable (cpu in millicores, memory in bytes), readiness, "
              "taints and topology (zone/region/instance_type promoted from well-known labels)."),
    ),
    "pods": dict(
        api="/api/v1", plural="pods", namespaced=True, pk="name", row=_row_pod,
        columns=[("name", "str"), ("namespace", "str"), ("app", "str"), ("node", "str"), ("phase", "str"),
                 ("ready", "str"), ("restarts", "int"), ("owner_kind", "str"), ("owner_name", "str"),
                 ("qos_class", "str"), ("pod_ip", "str"), ("service_account", "str"),
                 ("priority_class", "str"), ("started_at", _TS), ("created", _TS)],
        fks=[("node", "nodes", "name")],
        desc=("Pods. ready is 'ready/total' containers; restarts is the SUM across containers; phase=Running "
              "does not mean healthy — see the containers table for per-container state/reason. "
              "app is promoted from app.kubernetes.io/name (fallback app)."),
    ),
    "containers": dict(
        api="/api/v1", plural="pods", namespaced=True, pk=None, row=None, metadata=False,
        columns=[("pod", "str"), ("namespace", "str"), ("name", "str"), ("image", "str"), ("init", "bool"),
                 ("ready", "bool"), ("state", "str"), ("reason", "str"), ("exit_code", "int"),
                 ("restarts", "int"), ("last_state_reason", "str"), ("last_exit_code", "int"),
                 ("cpu_request_millicores", "int"), ("cpu_limit_millicores", "int"),
                 ("memory_request_bytes", "int"), ("memory_limit_bytes", "int")],
        fks=[("pod", "pods", "name")],
        desc=("One row per container of every pod (derived from pods). state running|waiting|terminated; "
              "reason carries CrashLoopBackOff / OOMKilled / ImagePullBackOff / Error; last_state_reason is "
              "why the previous run ended. Requests/limits normalized (millicores, bytes)."),
    ),
    "deployments": dict(
        api="/apis/apps/v1", plural="deployments", namespaced=True, pk="name", row=_row_deployment,
        columns=[("name", "str"), ("namespace", "str"), ("replicas", "int"), ("ready", "int"), ("updated", "int"),
                 ("available", "int"), ("unavailable", "int"), ("strategy", "str"), ("selector", _JS),
                 ("images", "str"), ("conditions", _JS), ("created", _TS)],
        fks=[],
        desc="Deployments with rollout counters (replicas vs ready/updated/available) and conditions.",
    ),
    "replicasets": dict(
        api="/apis/apps/v1", plural="replicasets", namespaced=True, pk="name", row=_row_replicaset,
        columns=[("name", "str"), ("namespace", "str"), ("owner_name", "str"), ("replicas", "int"),
                 ("ready", "int"), ("available", "int"), ("revision", "str"), ("images", "str"), ("created", _TS)],
        fks=[("owner_name", "deployments", "name")],
        desc="ReplicaSets (pod → replicaset → deployment is the owner chain). revision = rollout number.",
    ),
    "statefulsets": dict(
        api="/apis/apps/v1", plural="statefulsets", namespaced=True, pk="name", row=_row_statefulset,
        columns=[("name", "str"), ("namespace", "str"), ("replicas", "int"), ("ready", "int"), ("current", "int"),
                 ("updated", "int"), ("service_name", "str"), ("update_strategy", "str"), ("images", "str"),
                 ("created", _TS)],
        fks=[("service_name", "services", "name")],
        desc="StatefulSets.",
    ),
    "daemonsets": dict(
        api="/apis/apps/v1", plural="daemonsets", namespaced=True, pk="name", row=_row_daemonset,
        columns=[("name", "str"), ("namespace", "str"), ("desired", "int"), ("current", "int"), ("ready", "int"),
                 ("available", "int"), ("misscheduled", "int"), ("images", "str"), ("created", _TS)],
        fks=[],
        desc="DaemonSets. desired vs ready tells you which nodes are missing the agent.",
    ),
    "jobs": dict(
        api="/apis/batch/v1", plural="jobs", namespaced=True, pk="name", row=_row_job,
        columns=[("name", "str"), ("namespace", "str"), ("owner_name", "str"), ("active", "int"),
                 ("succeeded", "int"), ("failed", "int"), ("completions", "int"), ("parallelism", "int"),
                 ("start_time", _TS), ("completion_time", _TS), ("conditions", _JS), ("created", _TS)],
        fks=[("owner_name", "cronjobs", "name")],
        desc="Jobs. owner_name is the CronJob for scheduled jobs.",
    ),
    "cronjobs": dict(
        api="/apis/batch/v1", plural="cronjobs", namespaced=True, pk="name", row=_row_cronjob,
        columns=[("name", "str"), ("namespace", "str"), ("schedule", "str"), ("suspend", "bool"),
                 ("concurrency_policy", "str"), ("active_count", "int"), ("last_schedule_time", _TS),
                 ("last_successful_time", _TS), ("created", _TS)],
        fks=[],
        desc="CronJobs. last_schedule_time vs last_successful_time shows a schedule that keeps failing.",
    ),
    "horizontal_pod_autoscalers": dict(
        api="/apis/autoscaling/v2", plural="horizontalpodautoscalers", namespaced=True, pk="name", row=_row_hpa,
        columns=[("name", "str"), ("namespace", "str"), ("target_kind", "str"), ("target_name", "str"),
                 ("min_replicas", "int"), ("max_replicas", "int"), ("current_replicas", "int"),
                 ("desired_replicas", "int"), ("current_metrics", _JS), ("conditions", _JS), ("created", _TS)],
        fks=[("target_name", "deployments", "name")],
        desc="HorizontalPodAutoscalers: target, bounds, current vs desired replicas, and the metrics driving them.",
    ),
    "configmaps": dict(
        api="/api/v1", plural="configmaps", namespaced=True, pk="name", row=_row_configmap,
        columns=[("name", "str"), ("namespace", "str"), ("keys", "str"), ("key_count", "int"), ("created", _TS)],
        fks=[],
        desc="ConfigMaps — KEY NAMES ONLY, values are never exposed (they routinely hold connection strings).",
    ),
    "resource_quotas": dict(
        api="/api/v1", plural="resourcequotas", namespaced=True, pk="name", row=_row_resource_quota,
        columns=[("name", "str"), ("namespace", "str"), ("hard", _JS), ("used", _JS), ("scopes", "str"),
                 ("created", _TS)],
        fks=[],
        desc="ResourceQuotas: hard limits vs used per namespace — a common cause of Pending pods.",
    ),
    "events": dict(
        api="/api/v1", plural="events", namespaced=True, pk=None, row=_row_event,
        columns=[("namespace", "str"), ("type", "str"), ("reason", "str"), ("message", "str"),
                 ("involved_kind", "str"), ("involved_name", "str"), ("involved_namespace", "str"),
                 ("source_component", "str"), ("reporting_controller", "str"), ("count", "int"),
                 ("first_seen", _TS), ("last_seen", _TS)],
        fks=[("involved_name", "pods", "name")],
        desc=("Events (type Normal|Warning) with the involved object. RETAINED ONLY ~1 HOUR by default and "
              "returned unsorted — sort by last_seen; use `since` to bound. Older history must come from "
              "logs (previous=true) or the workload's conditions."),
    ),
    # ── networking ──
    "services": dict(
        api="/api/v1", plural="services", namespaced=True, pk="name", row=_row_service,
        columns=[("name", "str"), ("namespace", "str"), ("type", "str"), ("cluster_ip", "str"),
                 ("external_ips", "str"), ("ports", _JS), ("selector", _JS), ("load_balancer_ingress", "str"),
                 ("created", _TS)],
        fks=[],
        desc=("Services. A Service picks pods by `selector` (a LABEL match, not a foreign key) — verify it has "
              "ready backends via endpoint_slices."),
    ),
    "endpoint_slices": dict(
        api="/apis/discovery.k8s.io/v1", plural="endpointslices", namespaced=True, pk="name", row=_row_endpoint_slice,
        columns=[("name", "str"), ("namespace", "str"), ("service", "str"), ("address_type", "str"),
                 ("addresses", "str"), ("ready_count", "int"), ("not_ready_count", "int"), ("ports", _JS)],
        fks=[("service", "services", "name")],
        desc="EndpointSlices: the pods actually backing a service and whether they are ready.",
    ),
    "ingresses": dict(
        api="/apis/networking.k8s.io/v1", plural="ingresses", namespaced=True, pk="name", row=_row_ingress,
        columns=[("name", "str"), ("namespace", "str"), ("ingress_class", "str"), ("hosts", "str"),
                 ("rules", _JS), ("tls_hosts", "str"), ("load_balancer_ingress", "str"), ("created", _TS)],
        fks=[("ingress_class", "ingress_classes", "name")],
        desc="Ingresses: host/path → service:port rules, TLS hosts and the ingress class routing them.",
    ),
    "ingress_classes": dict(
        api="/apis/networking.k8s.io/v1", plural="ingressclasses", namespaced=False, pk="name", row=_row_ingress_class,
        columns=[("name", "str"), ("controller", "str"), ("is_default", "bool"), ("parameters", _JS), ("created", _TS)],
        fks=[],
        desc="IngressClasses: which controller serves an ingress; no default class explains an ingress that does nothing.",
    ),
    "network_policies": dict(
        api="/apis/networking.k8s.io/v1", plural="networkpolicies", namespaced=True, pk="name", row=_row_network_policy,
        columns=[("name", "str"), ("namespace", "str"), ("pod_selector", _JS), ("policy_types", "str"),
                 ("ingress_rules", _JS), ("egress_rules", _JS), ("created", _TS)],
        fks=[],
        desc="NetworkPolicies: pod selector and ingress/egress rules — the answer to 'A cannot reach B'.",
    ),
    # ── storage ──
    "persistent_volume_claims": dict(
        api="/api/v1", plural="persistentvolumeclaims", namespaced=True, pk="name", row=_row_pvc,
        columns=[("name", "str"), ("namespace", "str"), ("phase", "str"), ("volume_name", "str"),
                 ("storage_class", "str"), ("access_modes", "str"), ("requested_bytes", "int"),
                 ("capacity_bytes", "int"), ("volume_mode", "str"), ("created", _TS)],
        fks=[("volume_name", "persistent_volumes", "name"), ("storage_class", "storage_classes", "name")],
        desc="PersistentVolumeClaims. phase Pending|Bound|Lost. Chain: claim → persistent_volumes → storage_classes → csi_drivers.",
    ),
    "persistent_volumes": dict(
        api="/api/v1", plural="persistentvolumes", namespaced=False, pk="name", row=_row_pv,
        columns=[("name", "str"), ("phase", "str"), ("capacity_bytes", "int"), ("access_modes", "str"),
                 ("reclaim_policy", "str"), ("storage_class", "str"), ("volume_mode", "str"),
                 ("claim_namespace", "str"), ("claim_name", "str"), ("source_type", "str"),
                 ("csi_driver", "str"), ("csi_volume_handle", "str"), ("created", _TS)],
        fks=[("storage_class", "storage_classes", "name"), ("csi_driver", "csi_drivers", "name"),
             ("claim_name", "persistent_volume_claims", "name")],
        desc="PersistentVolumes. phase Available|Bound|Released|Failed; reclaim_policy Retain|Delete; source_type csi|nfs|hostPath|….",
    ),
    "storage_classes": dict(
        api="/apis/storage.k8s.io/v1", plural="storageclasses", namespaced=False, pk="name", row=_row_storage_class,
        columns=[("name", "str"), ("provisioner", "str"), ("reclaim_policy", "str"), ("volume_binding_mode", "str"),
                 ("allow_volume_expansion", "bool"), ("is_default", "bool"), ("parameters", _JS), ("created", _TS)],
        fks=[("provisioner", "csi_drivers", "name")],
        desc="StorageClasses. volume_binding_mode WaitForFirstConsumer vs Immediate explains claims that stay Pending until a pod schedules.",
    ),
    "csi_drivers": dict(
        api="/apis/storage.k8s.io/v1", plural="csidrivers", namespaced=False, pk="name", row=_row_csi_driver,
        columns=[("name", "str"), ("attach_required", "bool"), ("pod_info_on_mount", "bool"),
                 ("volume_lifecycle_modes", "str"), ("storage_capacity", "bool"), ("fs_group_policy", "str"),
                 ("requires_republish", "bool"), ("created", _TS)],
        fks=[],
        desc="Installed CSI drivers — which storage vendors this cluster actually has.",
    ),
    # ── custom resources ──
    "custom_resource_definitions": dict(
        api="/apis/apiextensions.k8s.io/v1", plural="customresourcedefinitions", namespaced=False, pk="name", row=_row_crd,
        columns=[("name", "str"), ("group", "str"), ("kind", "str"), ("plural", "str"), ("scope", "str"),
                 ("served_versions", "str"), ("storage_version", "str"), ("established", "bool"), ("created", _TS)],
        fks=[],
        desc="Installed CustomResourceDefinitions — what operators this cluster runs. Populated kinds also appear as crd::<group>/<Kind> tables.",
    ),
}

# Runtime tables (not object catalogs; emitted conditionally / on demand).
_RUNTIME_TABLES: Dict[str, dict] = {
    "pod_metrics": dict(
        columns=[("namespace", "str"), ("pod", "str"), ("container", "str"), ("cpu_millicores", "int"),
                 ("memory_bytes", "int"), ("timestamp", _TS), ("window_seconds", "int")],
        pk=None, fks=[("pod", "pods", "name")],
        desc="CURRENT cpu/memory usage per container from metrics-server (a snapshot, no history). Join to containers for limits → utilisation.",
    ),
    "node_metrics": dict(
        columns=[("node", "str"), ("cpu_millicores", "int"), ("memory_bytes", "int"), ("timestamp", _TS),
                 ("window_seconds", "int")],
        pk="node", fks=[("node", "nodes", "name")],
        desc="CURRENT cpu/memory usage per node from metrics-server (a snapshot). Join to nodes (allocatable) for utilisation %.",
    ),
    "logs": dict(
        columns=[("namespace", "str"), ("pod", "str"), ("container", "str"), ("timestamp", _TS), ("line", "str")],
        pk=None, fks=[("pod", "pods", "name")],
        desc=("Pod logs, one row per line. REQUIRES `pod` (+ `namespace`) or a `label_selector` (fanned out to a "
              "few pods). Optional: container, since ('-1h'), tail_lines (default 500), previous=true for the "
              "crashed container's last run, grep (regex)."),
    ),
}

_METADATA_COLUMNS = [("labels", _JS), ("annotations", _JS)]


# ── the client ────────────────────────────────────────────────────────────────


class KubernetesClient(DataSourceClient):
    """Kubernetes API server as a table-shaped, read-only data source."""

    capabilities = {Capability.QUERY}

    relative_date_hint = (
        "Relative dates (Kubernetes): `since` in the query spec takes relative offsets "
        "('-1h', '-24h', '-7d') or seconds and is evaluated at execution time — never "
        "hard-code a timestamp. Object timestamps (created, last_seen, …) are UTC datetimes."
    )

    def __init__(
        self,
        access_file: str = "",
        namespaces: Any = None,
        verify_ssl: bool = True,
        discover_crds: bool = True,
        max_crd_tables: int = 40,
        log_tail_default: int = 500,
        log_tail_max: int = 5000,
        max_log_pods: int = 10,
        request_timeout: int = 60,
        **kwargs: Any,
    ):
        self.api_server_url, self.ca_pem, self._token = parse_access_file(access_file)
        self.namespaces: List[str] = self._normalize_namespaces(namespaces)
        self.verify_ssl = bool(verify_ssl)
        self.discover_crds = bool(discover_crds)
        self.max_crd_tables = max(0, int(max_crd_tables or 0))
        # The hard cap wins: a max below the default clamps the default down.
        self.log_tail_max = max(1, int(log_tail_max or 5000))
        self.log_tail_default = min(max(1, int(log_tail_default or 500)), self.log_tail_max)
        self.max_log_pods = max(1, int(max_log_pods or 10))
        self.request_timeout = max(1, int(request_timeout or 60))
        self._ca_path: Optional[str] = None
        # Per-instance discovery memo: CRD table name → (path template, namespaced).
        self._crd_tables: Dict[str, dict] = {}
        self._metrics_available: Optional[bool] = None

    @staticmethod
    def _normalize_namespaces(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, str):
            parts = [p.strip() for p in re.split(r"[,\s]+", value)]
        else:
            parts = [str(p).strip() for p in value]
        return [_check_segment(p, "namespace") for p in parts if p]

    @property
    def description(self):
        text = ("Kubernetes client — investigate a cluster: workloads, nodes, events, networking, storage, "
                "custom resources, live usage and pod logs via the API server (read-only).")
        return text + "\n\n" + self.system_prompt()

    # ── transport ────────────────────────────────────────────────────────────

    def _configuration(self):
        cfg = Configuration()
        cfg.host = self.api_server_url
        # The full header value, not api_key + api_key_prefix: the generated
        # client's prefix handling has changed across releases (observed: the
        # raw token sent with no "Bearer " → the API server treats the call as
        # system:anonymous). Setting the value verbatim is version-proof.
        cfg.api_key = {"authorization": f"Bearer {self._token}"}
        cfg.verify_ssl = self.verify_ssl
        if self.ca_pem and self.verify_ssl:
            if not self._ca_path or not os.path.exists(self._ca_path):
                fd, path = tempfile.mkstemp(prefix="bow-k8s-ca-", suffix=".pem")
                with os.fdopen(fd, "w") as fh:
                    fh.write(self.ca_pem)
                self._ca_path = path
            cfg.ssl_ca_cert = self._ca_path
        return cfg

    @contextmanager
    def connect(self) -> Generator[Any, None, None]:
        # Module-level `ApiClient` so tests fake the HTTP boundary by patching it.
        api = ApiClient(self._configuration())
        try:
            yield api
        finally:
            try:
                api.close()
            except Exception:
                pass

    def __del__(self):
        path = getattr(self, "_ca_path", None)
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _assert_path_allowed(path: str) -> None:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("`path` must be an absolute API path such as /apis/apps/v1/deployments.")
        if any(ch in path for ch in "?#%"):
            raise ValueError(
                f"Path '{path[:120]}' is not allowed: query strings, fragments and percent-escapes are "
                "refused — pass query parameters in `params`."
            )
        raw_segments = [s for s in path.split("/") if s]
        for seg in raw_segments:
            _check_segment(seg, "path")
        segments = [s.lower() for s in raw_segments]
        bad = _FORBIDDEN_SEGMENTS.intersection(segments)
        if bad:
            raise ValueError(
                f"Path '{path}' is not allowed: '{sorted(bad)[0]}' is refused by this connector "
                "(Secrets and workload subresources are never read)."
            )

    def _get(self, api, path: str, params: Optional[dict] = None, *, accept: str = "application/json") -> Any:
        """One GET. Returns parsed JSON (or text for non-JSON bodies). Raises a
        readable RuntimeError on HTTP/TLS/transport errors."""
        self._assert_path_allowed(path)
        query = [(k, v) for k, v in (params or {}).items() if v is not None and v != ""]
        try:
            resp = api.call_api(
                path, "GET",
                query_params=query,
                header_params={"Accept": accept},
                auth_settings=["BearerToken"],
                _preload_content=False,
                _return_http_data_only=True,
                _request_timeout=self.request_timeout,
            )
        except ApiException as e:
            raise RuntimeError(self._describe_api_error(e, path)) from None
        except Exception as e:  # urllib3 transport errors surface here
            name = type(e).__name__
            msg = str(e)
            if "SSL" in name or "CERTIFICATE" in msg.upper() or "certificate" in msg:
                raise RuntimeError(
                    f"TLS error talking to {self.api_server_url}: {msg[:300]}. The CA inside the access "
                    "file does not match the API server — re-run print_access_file.sh (the cluster CA may "
                    "have been rotated)."
                ) from None
            raise RuntimeError(
                f"Could not reach the Kubernetes API server at {self.api_server_url}: {msg[:300]}. "
                "The backend must be able to reach the `server` URL in the access file (private "
                "endpoints are only reachable from inside their network)."
            ) from None

        data = resp.data
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        if accept == "application/json":
            try:
                return json.loads(data) if data else {}
            except ValueError:
                raise RuntimeError(f"Kubernetes API returned non-JSON for {path}: {str(data)[:200]}")
        return data

    def _describe_api_error(self, e, path: str) -> str:
        status = getattr(e, "status", None)
        body = getattr(e, "body", None) or ""
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        message = ""
        try:
            message = (json.loads(body) or {}).get("message") or ""
        except (ValueError, AttributeError):
            message = str(body)[:300]
        if status == 401:
            return ("Kubernetes rejected the token (401 Unauthorized): it has expired or been revoked. "
                    "Re-run the setup guide (apply rbac.yaml, then print_access_file.sh) and paste the new "
                    "access file.")
        if status == 403:
            return (f"Kubernetes refused {path} (403 Forbidden): {message or 'RBAC denied the request'}. "
                    "The service account must have the cluster-wide read-only ClusterRole from rbac.yaml.")
        if status == 404:
            # A named object that does not exist reports NotFound with a message
            # naming it ('pods "x" not found'); an unserved API group reports the
            # generic 'the server could not find the requested resource'.
            if message and "could not find the requested resource" not in message:
                return f"Kubernetes object not found (404): {message}"
            return (f"Kubernetes has no API at {path} (404): the API group is not served by this cluster "
                    "(missing metrics-server / operator, or an older Kubernetes version).")
        if status == 0 or status is None:
            return f"Could not reach the Kubernetes API server at {self.api_server_url}: {getattr(e, 'reason', e)}"
        return f"Kubernetes API error {status} on {path}: {message or getattr(e, 'reason', '')}"

    def _list(self, api, path: str, params: Optional[dict] = None, limit: int = DEFAULT_LIMIT) -> List[dict]:
        """Paginated list (`limit` + `continue`) capped at `limit` items."""
        items: List[dict] = []
        cont: Optional[str] = None
        for _ in range(MAX_PAGES):
            remaining = limit - len(items)
            if remaining <= 0:
                break
            q = dict(params or {})
            q["limit"] = min(PAGE_SIZE, remaining)
            if cont:
                q["continue"] = cont
            body = self._get(api, path, q)
            items.extend(body.get("items") or [])
            cont = (body.get("metadata") or {}).get("continue")
            if not cont:
                break
        return items[:limit]

    # ── connection test ───────────────────────────────────────────────────────

    def test_connection(self):
        try:
            with self.connect() as api:
                version = self._get(api, "/version")
                server_version = version.get("gitVersion") or f"{version.get('major')}.{version.get('minor')}"
                try:
                    nodes = self._list(api, "/api/v1/nodes", limit=PAGE_SIZE)
                except RuntimeError as e:
                    if "403" in str(e):
                        return {
                            "success": False,
                            "message": (
                                f"Connected to Kubernetes {server_version}, but the token cannot list nodes "
                                "(403). It is not cluster-wide read-only — someone bound a namespaced or "
                                "`view` role instead of tools/kubernetes/rbac.yaml. Apply rbac.yaml and "
                                "re-run print_access_file.sh."
                            ),
                        }
                    raise
                namespaces = self._list(api, "/api/v1/namespaces", limit=PAGE_SIZE)
                metrics = self._probe_metrics(api)
                self._metrics_available = metrics
            visible = ""
            if self.namespaces:
                visible = f", scoped to namespaces {', '.join(self.namespaces)}"
            usage = "metrics-server available" if metrics else "no metrics-server (usage tables hidden)"
            return {
                "success": True,
                "message": (f"Connected to Kubernetes {server_version}: {len(nodes)} nodes, "
                            f"{len(namespaces)} namespaces{visible}; {usage}."),
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _probe_metrics(self, api) -> bool:
        try:
            body = self._get(api, METRICS_GROUP_PATH)
            return bool(body)
        except RuntimeError:
            return False

    # ── schema discovery ──────────────────────────────────────────────────────

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        tables = [self._build_fixed(name) for name in _CATALOG]
        with self.connect() as api:
            metrics = self._probe_metrics(api)
            self._metrics_available = metrics
            if metrics:
                tables.append(self._build_runtime("pod_metrics"))
                tables.append(self._build_runtime("node_metrics"))
            else:
                idx = next(i for i, t in enumerate(tables) if t.name == "nodes")
                tables[idx].description = (tables[idx].description or "") + \
                    " (This cluster has no metrics-server, so pod_metrics/node_metrics are unavailable.)"
            tables.append(self._build_runtime("logs"))
            if self.discover_crds and self.max_crd_tables > 0:
                try:
                    tables.extend(self._discover_crd_tables(api, tables, progress_callback))
                except Exception as e:  # never fail the whole catalog on CRD trouble
                    logger.warning("kubernetes: CRD discovery failed: %s", e)
        return tables

    def get_schema(self, table_name: str) -> Table:
        if table_name in _CATALOG:
            return self._build_fixed(table_name)
        if table_name in _RUNTIME_TABLES:
            return self._build_runtime(table_name)
        if table_name.startswith(CRD_TABLE_PREFIX):
            with self.connect() as api:
                spec = self._resolve_crd_table(api, table_name)
            return self._build_crd_table(spec)
        raise ValueError(
            f"Unknown Kubernetes table '{table_name}'. Available: {', '.join(self.table_names())} "
            f"plus discovered {CRD_TABLE_PREFIX}<group>/<Kind> tables."
        )

    @staticmethod
    def table_names() -> List[str]:
        return list(_CATALOG) + list(_RUNTIME_TABLES)

    def _make_table(self, name: str, desc: str, columns: Iterable[Tuple[str, str]], pk: Optional[str],
                    fks: Iterable[Tuple[str, str, str]], metadata: bool = True) -> Table:
        cols = [TableColumn(name=c, dtype=d) for c, d in columns]
        if metadata:
            cols.extend(TableColumn(name=c, dtype=d) for c, d in _METADATA_COLUMNS)
        by_name = {c.name: c for c in cols}
        fk_objs = [
            ForeignKey(column=by_name[col], references_name=ref_table,
                       references_column=TableColumn(name=ref_col, dtype="str"))
            for col, ref_table, ref_col in fks if col in by_name
        ]
        pks = [by_name[pk]] if pk and pk in by_name else []
        return Table(name=name, description=desc, columns=cols, pks=pks, fks=fk_objs)

    def _build_fixed(self, name: str) -> Table:
        spec = _CATALOG[name]
        return self._make_table(name, spec["desc"], spec["columns"], spec["pk"], spec["fks"],
                                metadata=spec.get("metadata", True))

    def _build_runtime(self, name: str) -> Table:
        spec = _RUNTIME_TABLES[name]
        return self._make_table(name, spec["desc"], spec["columns"], spec["pk"], spec["fks"], metadata=False)

    def _build_crd_table(self, spec: dict) -> Table:
        columns = [("name", "str"), ("namespace", "str"), ("generation", "int"), ("created", _TS),
                   ("conditions", _JS), ("spec", _JS), ("status", _JS)]
        if not spec["namespaced"]:
            columns = [c for c in columns if c[0] != "namespace"]
        keys = spec.get("spec_keys") or []
        desc = (f"Custom resource {spec['kind']} ({spec['group']}/{spec['version']}, "
                f"{'namespaced' if spec['namespaced'] else 'cluster-scoped'}). spec/status are JSON.")
        if keys:
            desc += f" Top-level spec keys: {', '.join(keys)}."
        return self._make_table(spec["table"], desc, columns, "name", [])

    @staticmethod
    def _crd_specs(crds: List[dict]) -> List[dict]:
        """CRD objects → candidate table specs (one per CRD, preferred version)."""
        specs = []
        for crd in crds:
            s = crd.get("spec") or {}
            group, names = s.get("group") or "", s.get("names") or {}
            if not group or any(group == g or group.endswith("." + g) for g in _INTERNAL_CRD_GROUPS):
                continue
            versions = s.get("versions") or []
            version = next((v.get("name") for v in versions if v.get("storage")), None) or \
                next((v.get("name") for v in versions if v.get("served")), None)
            if not version or not names.get("plural") or not names.get("kind"):
                continue
            specs.append({
                "table": f"{CRD_TABLE_PREFIX}{group}/{names['kind']}",
                "group": group, "version": version, "kind": names["kind"], "plural": names["plural"],
                "namespaced": s.get("scope") == "Namespaced",
                "api": f"/apis/{group}/{version}",
            })
        return sorted(specs, key=lambda x: x["table"])

    def _discover_crd_tables(self, api, tables: List[Table], progress_callback) -> List[Table]:
        crds = self._list(api, "/apis/apiextensions.k8s.io/v1/customresourcedefinitions", limit=MAX_ROWS)
        specs = self._crd_specs(crds)[:CRD_PROBE_CAP]
        emitted: List[Table] = []
        skipped: List[str] = []
        total = len(specs)
        for i, spec in enumerate(specs):
            if progress_callback:
                progress_callback("custom resources", spec["table"], i, total)
            try:
                # Same scope as every later query: a kind populated only outside
                # the allowlist would otherwise be advertised and always return empty.
                probe = self._list_objects(api, spec["api"], spec["plural"], spec["namespaced"],
                                           list(self.namespaces) or None, {}, limit=1)
            except RuntimeError:
                continue
            if not probe:
                continue
            spec["spec_keys"] = sorted((probe[0].get("spec") or {}).keys())[:12]
            self._crd_tables[spec["table"]] = spec
            if len(emitted) < self.max_crd_tables:
                emitted.append(self._build_crd_table(spec))
            else:
                skipped.append(spec["table"])
        if skipped:
            idx = next(i for i, t in enumerate(tables) if t.name == "custom_resource_definitions")
            tables[idx].description = (tables[idx].description or "") + (
                f" {len(skipped)} more populated kinds exist beyond the max_crd_tables cap and can be "
                f"queried by table name: {', '.join(skipped[:30])}{'…' if len(skipped) > 30 else ''}."
            )
        return emitted

    def _resolve_crd_table(self, api, table_name: str) -> dict:
        if table_name in self._crd_tables:
            return self._crd_tables[table_name]
        rest = table_name[len(CRD_TABLE_PREFIX):]
        if "/" not in rest:
            raise ValueError(f"CRD table name must be '{CRD_TABLE_PREFIX}<group>/<Kind>', got '{table_name}'.")
        group, kind = rest.split("/", 1)
        crds = self._list(api, "/apis/apiextensions.k8s.io/v1/customresourcedefinitions", limit=MAX_ROWS)
        for spec in self._crd_specs(crds):
            if spec["group"] == group and spec["kind"] == kind:
                self._crd_tables[table_name] = spec
                return spec
        raise ValueError(f"No CustomResourceDefinition for '{kind}' in group '{group}' on this cluster.")

    # ── querying ──────────────────────────────────────────────────────────────

    def execute_query(self, query) -> pd.DataFrame:
        """Execute a Kubernetes query spec (JSON string or dict) — see `system_prompt`."""
        spec = self._parse_spec(query)
        limit = min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS)
        with self.connect() as api:
            if spec.get("path"):
                return self._query_path(api, spec, limit)
            table = spec["table"]
            if table == "logs":
                return self._query_logs(api, spec)
            if table in ("pod_metrics", "node_metrics"):
                return self._query_metrics(api, table, spec, limit)
            if table.startswith(CRD_TABLE_PREFIX):
                crd = self._resolve_crd_table(api, table)
                objs = self._fetch_objects(api, crd["api"], crd["plural"], crd["namespaced"], spec, limit)
                rows = [self._finish_row(_row_custom(o), o, spec) for o in objs]
                if not crd["namespaced"]:
                    for r in rows:
                        r.pop("namespace", None)
                crd_cols = [c.name for c in self._build_crd_table(crd).columns] + (["raw"] if spec.get("raw") else [])
                return self._frame(rows, crd_cols)
            cat = _CATALOG[table]
            objs = self._fetch_objects(api, cat["api"], cat["plural"], cat["namespaced"], spec, limit)
            if table == "containers":
                rows = [r for o in objs for r in _container_rows(o)]
                return self._frame(rows[:limit], self._columns_of(cat, metadata=False))
            if table == "events":
                objs = self._filter_events(objs, spec.get("since"))
            rows = [self._finish_row(cat["row"](o), o, spec) for o in objs]
            return self._frame(rows, self._columns_of(cat, metadata=cat.get("metadata", True), raw=bool(spec.get("raw"))))

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = dict(query)
        else:
            try:
                spec = json.loads(query)
            except (TypeError, json.JSONDecodeError):
                raise ValueError(
                    "Kubernetes query must be a JSON object like "
                    '{"table": "pods", "namespace": "payments", "label_selector": "app=checkout", "limit": 200} — got: '
                    f"{str(query)[:200]}"
                )
        if not isinstance(spec, dict) or not (spec.get("table") or spec.get("path")):
            raise ValueError('Kubernetes query spec must include a "table" (or an escape-hatch "path") key.')
        table = spec.get("table")
        if table and table not in _CATALOG and table not in _RUNTIME_TABLES and not str(table).startswith(CRD_TABLE_PREFIX):
            raise ValueError(
                f"Unknown Kubernetes table '{table}'. Available: {', '.join(self.table_names())} "
                f"plus discovered {CRD_TABLE_PREFIX}<group>/<Kind> tables."
            )
        if table in ("pod_metrics", "node_metrics") and self._metrics_available is False:
            raise ValueError(f"'{table}' is unavailable: this cluster has no metrics-server.")
        return spec

    def _resolve_namespaces(self, spec: dict) -> Optional[List[str]]:
        """None → all namespaces in one call; list → one call per namespace."""
        raw = spec.get("namespace")
        if raw in (None, "", "all", "*"):
            return list(self.namespaces) if self.namespaces else None
        wanted = self._normalize_namespaces(raw)
        if self.namespaces:
            outside = [n for n in wanted if n not in self.namespaces]
            if outside:
                raise ValueError(
                    f"Namespace(s) {', '.join(outside)} are outside this connection's allowlist "
                    f"({', '.join(self.namespaces)})."
                )
        return wanted

    def _list_objects(self, api, api_path: str, plural: str, namespaced: bool,
                      namespaces: Optional[List[str]], params: dict, limit: int) -> List[dict]:
        if not namespaced or namespaces is None:
            return self._list(api, f"{api_path}/{plural}", params, limit=limit)
        items: List[dict] = []
        for ns in namespaces:
            if len(items) >= limit:
                break
            items.extend(self._list(api, f"{api_path}/namespaces/{ns}/{plural}", params, limit=limit - len(items)))
        return items

    def _fetch_objects(self, api, api_path: str, plural: str, namespaced: bool, spec: dict, limit: int) -> List[dict]:
        params = {"labelSelector": spec.get("label_selector"), "fieldSelector": spec.get("field_selector")}
        namespaces = self._resolve_namespaces(spec) if namespaced else None
        name = spec.get("name")
        if name:
            _check_segment(name, "name")
            if not namespaced:
                return [self._get(api, f"{api_path}/{plural}/{name}")]
            if namespaces and len(namespaces) == 1:
                return [self._get(api, f"{api_path}/namespaces/{namespaces[0]}/{plural}/{name}")]
            params["fieldSelector"] = ",".join(filter(None, [params["fieldSelector"], f"metadata.name={name}"]))
        return self._list_objects(api, api_path, plural, namespaced, namespaces, params, limit)

    def _finish_row(self, row: dict, obj: dict, spec: dict) -> dict:
        meta = _meta(obj)
        row["labels"] = _json(meta.get("labels") or {})
        row["annotations"] = _json(clean_annotations(meta.get("annotations")))
        if spec.get("raw"):
            row["raw"] = _json(redact_object(obj))
        return row

    @staticmethod
    def _frame(rows: List[dict], columns: Optional[List[str]] = None) -> pd.DataFrame:
        """Rows → DataFrame. An EMPTY result still carries the table's columns so
        downstream code (and the agent) can tell 'no objects' from 'no such
        columns'."""
        if not rows and columns:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(rows)

    @staticmethod
    def _columns_of(spec: dict, metadata: bool = True, raw: bool = False) -> List[str]:
        cols = [c for c, _ in spec["columns"]]
        if metadata:
            cols += [c for c, _ in _METADATA_COLUMNS]
        if raw:
            cols.append("raw")
        return cols

    @staticmethod
    def _filter_events(objs: List[dict], since: Any) -> List[dict]:
        seconds = parse_duration_seconds(since)
        if not seconds:
            return objs
        cutoff = pd.Timestamp(datetime.now(timezone.utc)) - pd.Timedelta(seconds=seconds)
        kept = []
        for o in objs:
            last = parse_rfc3339(o.get("lastTimestamp") or o.get("eventTime") or _meta(o).get("creationTimestamp"))
            if last is None or last >= cutoff:
                kept.append(o)
        return kept

    def _query_path(self, api, spec: dict, limit: int) -> pd.DataFrame:
        path = spec["path"]
        raw_params = spec.get("params") or {}
        if not isinstance(raw_params, dict):
            raise ValueError("`params` must be a JSON object of query parameters.")
        streaming = [k for k in raw_params if str(k).lower() in _STREAMING_PARAMS]
        if streaming:
            raise ValueError(
                f"Query parameter(s) {', '.join(sorted(streaming))} are refused: they turn the request "
                "into an open-ended stream. Use `limit` and re-query instead of watch/follow."
            )
        params = dict(raw_params)
        body = self._get(api, path, params) if "limit" in params else None
        if body is None:
            body = {}
            items: List[dict] = []
            cont = None
            for _ in range(MAX_PAGES):
                q = dict(params)
                q["limit"] = min(PAGE_SIZE, limit - len(items))
                if cont:
                    q["continue"] = cont
                body = self._get(api, path, q)
                if not isinstance(body, dict) or "items" not in body:
                    break
                items.extend(body.get("items") or [])
                cont = (body.get("metadata") or {}).get("continue")
                if not cont or len(items) >= limit:
                    break
            if isinstance(body, dict) and "items" in body:
                body = {"items": items[:limit]}
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            rows = []
            for o in body["items"]:
                if not isinstance(o, dict):
                    rows.append({"value": o})
                    continue
                meta = _meta(o)
                row = {"name": meta.get("name"), "namespace": meta.get("namespace"),
                       "kind": o.get("kind"), "created": parse_rfc3339(meta.get("creationTimestamp"))}
                for key in ("spec", "status", "data"):
                    if key in o:
                        row[key] = _json(o[key])
                rows.append(self._finish_row(row, o, spec))
            return self._frame(rows)
        if isinstance(body, dict):
            return pd.DataFrame([{k: (_json(v) if isinstance(v, (dict, list)) else v) for k, v in body.items()}])
        return pd.DataFrame({"result": [body]})

    def _query_metrics(self, api, table: str, spec: dict, limit: int) -> pd.DataFrame:
        if table == "node_metrics":
            items = self._list(api, f"{METRICS_GROUP_PATH}/nodes",
                               {"labelSelector": spec.get("label_selector")}, limit=limit)
            rows = [{
                "node": _meta(o).get("name"),
                "cpu_millicores": cpu_millicores((o.get("usage") or {}).get("cpu")),
                "memory_bytes": to_bytes((o.get("usage") or {}).get("memory")),
                "timestamp": parse_rfc3339(o.get("timestamp")),
                "window_seconds": parse_duration_seconds(o.get("window")),
            } for o in items]
            return self._frame(rows, [c for c, _ in _RUNTIME_TABLES["node_metrics"]["columns"]])
        namespaces = self._resolve_namespaces(spec)
        params = {"labelSelector": spec.get("label_selector"), "fieldSelector": spec.get("field_selector")}
        if spec.get("pod"):
            params["fieldSelector"] = ",".join(filter(None, [params["fieldSelector"], f"metadata.name={spec['pod']}"]))
        items = self._list_objects(api, METRICS_GROUP_PATH, "pods", True, namespaces, params, limit)
        rows = []
        for o in items:
            meta = _meta(o)
            for c in o.get("containers") or []:
                usage = c.get("usage") or {}
                rows.append({
                    "namespace": meta.get("namespace"), "pod": meta.get("name"), "container": c.get("name"),
                    "cpu_millicores": cpu_millicores(usage.get("cpu")), "memory_bytes": to_bytes(usage.get("memory")),
                    "timestamp": parse_rfc3339(o.get("timestamp")),
                    "window_seconds": parse_duration_seconds(o.get("window")),
                })
        return self._frame(rows[:limit], [c for c, _ in _RUNTIME_TABLES["pod_metrics"]["columns"]])

    def _query_logs(self, api, spec: dict) -> pd.DataFrame:
        pod, selector = spec.get("pod"), spec.get("label_selector")
        if not pod and not selector:
            raise ValueError('The "logs" table requires a "pod" name (with "namespace") or a "label_selector".')
        if pod:
            _check_segment(pod, "pod")
        namespaces = self._resolve_namespaces(spec)
        params = {"labelSelector": selector}
        if pod:
            params["fieldSelector"] = f"metadata.name={pod}"
        if pod and namespaces and len(namespaces) == 1:
            pods = [self._get(api, f"/api/v1/namespaces/{namespaces[0]}/pods/{pod}")]
        else:
            pods = self._list_objects(api, "/api/v1", "pods", True, namespaces, params, limit=self.max_log_pods)
        if not pods:
            raise ValueError("No pod matched the logs query" + (f" (pod={pod})" if pod else f" (label_selector={selector})") + ".")
        pods = pods[: self.max_log_pods]

        tail = min(int(spec.get("tail_lines") or self.log_tail_default), self.log_tail_max)
        since = parse_duration_seconds(spec.get("since"))
        previous = bool(spec.get("previous"))
        grep = None
        if spec.get("grep"):
            try:
                grep = re.compile(spec["grep"])
            except re.error as e:
                raise ValueError(f"`grep` is not a valid regular expression ({e}): {spec['grep']!r}") from None
        wanted_container = spec.get("container")

        rows: List[dict] = []
        for p in pods:
            meta = _meta(p)
            ns, name = meta.get("namespace"), meta.get("name")
            containers = [c.get("name") for c in (p.get("spec") or {}).get("containers") or []]
            targets = [wanted_container] if wanted_container else (containers or [None])
            for container in targets:
                q = {"timestamps": "true", "tailLines": tail, "container": container,
                     "sinceSeconds": since, "previous": "true" if previous else None}
                try:
                    # `text/plain, */*`, not a bare `text/plain`: the log subresource
                    # on some API servers (observed on microk8s v1.35.6) answers a
                    # bare text/plain with 406 "only application/json, yaml, protobuf
                    # are accepted", while */* — what kubectl sends — returns the
                    # text. Newer servers (kind v1.36) accept either.
                    text = self._get(api, f"/api/v1/namespaces/{ns}/pods/{name}/log", q, accept=LOG_ACCEPT)
                except RuntimeError as e:
                    # A container with no previous run / not started yet returns 400 — surface as a row.
                    rows.append({"namespace": ns, "pod": name, "container": container, "timestamp": None,
                                 "line": f"<no logs: {str(e)[:200]}>"})
                    continue
                for line in str(text or "").splitlines():
                    ts, _, msg = line.partition(" ")
                    stamp = parse_rfc3339(ts)
                    if stamp is None:
                        stamp, msg = None, line
                    if grep and not grep.search(msg):
                        continue
                    rows.append({"namespace": ns, "pod": name, "container": container, "timestamp": stamp, "line": msg})
        return self._frame(rows, [c for c, _ in _RUNTIME_TABLES["logs"]["columns"]])

    # ── prompts ───────────────────────────────────────────────────────────────

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self):
        return """
        ## Kubernetes Integration
        Query the cluster via `execute_query(query)` where `query` is a JSON string:

        ```json
        {"table": "pods",
         "namespace": "payments",
         "label_selector": "app=checkout,tier!=canary",
         "field_selector": "spec.nodeName=ip-10-0-1-5",
         "limit": 500}
        ```

        - `table` (required): a fixed table (namespaces, nodes, pods, containers, deployments,
          replicasets, statefulsets, daemonsets, jobs, cronjobs, horizontal_pod_autoscalers,
          configmaps, resource_quotas, events, services, endpoint_slices, ingresses,
          ingress_classes, network_policies, persistent_volume_claims, persistent_volumes,
          storage_classes, csi_drivers, custom_resource_definitions), a runtime table
          (pod_metrics, node_metrics, logs) or a discovered `crd::<group>/<Kind>` table.
        - `namespace` (optional): a name or a list; omit for ALL namespaces. Ignored for
          cluster-scoped kinds (nodes, persistent_volumes, storage_classes, …).
        - `name` (optional): fetch one object.
        - `label_selector` / `field_selector` (optional): passed to the API — FILTER HERE,
          server-side (`app=checkout`, `status.phase=Pending`), never by parsing the `labels`
          column in pandas. `labels`/`annotations` columns are JSON strings for reading,
          grouping and display.
        - `limit` (optional, default 500): rows; paginated server-side.
        - `since` (optional): '-1h' / '-24h' / seconds — bounds `events` (client-side on
          last_seen) and `logs` (server-side).
        - `raw: true` (optional): adds a `raw` column with the full object JSON (env literals
          redacted).
        - `logs` REQUIRES `pod` (+ `namespace`) or a `label_selector` (fanned out to a few pods):
          {"table": "logs", "namespace": "payments", "pod": "checkout-7d9f-abc12",
           "container": "app", "since": "-30m", "tail_lines": 500, "previous": false, "grep": "ERROR|timeout"}
        - Escape hatch — any GET list path:
          {"path": "/apis/apps/v1/namespaces/payments/deployments", "params": {"labelSelector": "app=x"}}.
          Secrets and exec/attach/portforward/proxy paths are refused.

        IMPORTANT specifics:
        - Quantities are normalized: cpu in MILLICORES (250m → 250, 2 → 2000), memory/storage
          in BYTES. Timestamps are UTC datetimes.
        - `pods.ready` is 'ready/total'; `pods.restarts` is the SUM across containers;
          phase=Running does not mean healthy. Use `containers` for per-container
          state/reason (CrashLoopBackOff, OOMKilled, ImagePullBackOff) and exit codes.
        - Owner chain: pod → replicaset (owner_kind/owner_name) → deployment (replicasets.owner_name).
          Storage chain: persistent_volume_claims.volume_name → persistent_volumes.storage_class
          → storage_classes.provisioner → csi_drivers.name.
        - A Service selects pods by LABELS (services.selector); confirm it has ready backends
          with endpoint_slices (service column). Ingress → ingress_classes via ingress_class.
        - `events` are retained ~1 hour by default and come unsorted: sort by last_seen and
          bound with `since`. For older failures use logs with previous=true.
        - `pod_metrics`/`node_metrics` are a CURRENT snapshot from metrics-server (no history);
          join to containers/nodes limits for utilisation %.
        - ConfigMap values and Secrets are never returned.
        - Aggregate by fetching rows and grouping in pandas.

        Examples:
        ```python
        # Crash-looping containers cluster-wide, with why
        df = client.execute_query('{"table": "containers", "limit": 2000}')
        bad = df[df["reason"].isin(["CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "Error"])]
        # Warning events for a namespace in the last hour, newest first
        ev = client.execute_query('{"table": "events", "namespace": "payments", "field_selector": "type=Warning", "since": "-1h", "limit": 500}')
        ev = ev.sort_values("last_seen", ascending=False)
        # Last 200 log lines of a crashed container's previous run
        logs = client.execute_query('{"table": "logs", "namespace": "payments", "pod": "checkout-7d9f-abc12", "previous": true, "tail_lines": 200}')
        # Why a claim is Pending: claim → class → driver
        pvc = client.execute_query('{"table": "persistent_volume_claims", "namespace": "payments", "field_selector": "status.phase=Pending"}')
        sc = client.execute_query('{"table": "storage_classes"}')
        ```
        """
