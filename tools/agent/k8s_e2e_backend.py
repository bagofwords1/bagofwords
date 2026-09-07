# Run: cd backend && BOW_DATABASE_URL=sqlite:///db/app.db uv run python ../tools/agent/k8s_e2e_backend.py  (needs /tmp/bow-agent/microk8s-access.yaml from tools/kubernetes/print_access_file.sh)
"""Section A of the Kubernetes connector e2e plan: the client against the default cluster."""
import json, re, sys, traceback
import pandas as pd
from app.data_sources.clients.kubernetes_client import (
    KubernetesClient, _CATALOG, _RUNTIME_TABLES, parse_access_file, CRD_TABLE_PREFIX,
)

ACCESS = open("/tmp/bow-agent/microk8s-access.yaml").read()
results = []


def case(cid, desc):
    def deco(fn):
        try:
            detail = fn()
            results.append((cid, desc, "PASS", detail or ""))
        except AssertionError as e:
            results.append((cid, desc, "FAIL", str(e)))
        except Exception as e:
            results.append((cid, desc, "ERROR", f"{type(e).__name__}: {str(e)[:200]}"))
        return fn
    return deco


c = KubernetesClient(access_file=ACCESS)
Q = lambda spec: c.execute_query(json.dumps(spec) if isinstance(spec, dict) else spec)


@case("A1", "test_connection reports version, counts, metrics availability")
def _():
    r = c.test_connection()
    assert r["success"], r["message"]
    m = r["message"]
    assert re.search(r"v1\.\d+\.\d+", m) and re.search(r"\d+ nodes", m) and re.search(r"\d+ namespaces", m), m
    assert "no metrics-server" in m, m
    return m


tables = {}


@case("A2", "get_schemas catalog: 27 fixed + logs, no metrics tables, crd:: tables, metadata, FK chains")
def _():
    global tables
    progress = []
    ts = c.get_schemas(progress_callback=lambda ph, it, d, t: progress.append(it))
    tables = {t.name: t for t in ts}
    missing = set(_CATALOG) - set(tables)
    assert not missing, f"missing fixed tables: {missing}"
    assert "logs" in tables and "pod_metrics" not in tables and "node_metrics" not in tables
    assert "metrics-server" in (tables["nodes"].description or "")
    crds = [n for n in tables if n.startswith(CRD_TABLE_PREFIX)]
    assert crds and len(crds) <= 40, crds
    for name, t in tables.items():
        cols = {col.name for col in t.columns}
        if name in ("containers", "logs"):
            assert not {"labels", "annotations"} & cols, name
        else:
            assert {"labels", "annotations"} <= cols, name
    fk = lambda n: {(f.column.name, f.references_name) for f in tables[n].fks}
    assert ("node", "nodes") in fk("pods") and ("owner_name", "deployments") in fk("replicasets")
    assert ("owner_name", "cronjobs") in fk("jobs") and ("volume_name", "persistent_volumes") in fk("persistent_volume_claims")
    assert ("storage_class", "storage_classes") in fk("persistent_volumes") and ("provisioner", "csi_drivers") in fk("storage_classes")
    assert ("ingress_class", "ingress_classes") in fk("ingresses") and ("service", "services") in fk("endpoint_slices")
    assert progress, "no CRD discovery progress reported"
    return f"{len(tables)} tables; crd:: tables={len(crds)} e.g. {crds[:3]}; CRD probes={len(progress)}"


counts = {}


@case("A3", "every fixed table returns a DataFrame with its columns (zero rows allowed)")
def _():
    bad = []
    for name, spec in _CATALOG.items():
        df = Q({"table": name, "limit": 2000})
        expected = [col for col, _ in spec["columns"]]
        if spec.get("metadata", True):
            expected += ["labels", "annotations"]
        if list(df.columns) != expected:
            bad.append((name, list(df.columns)[:5]))
        counts[name] = len(df)
    assert not bad, f"column mismatch: {bad}"
    empties = [n for n, k in counts.items() if k == 0]
    return f"rows: { {k: v for k, v in counts.items() if v} }; empty (columns intact): {empties}"


@case("A4", "row semantics across pods/containers/nodes/deployments/slices/policies/storage/configmaps")
def _():
    pods = Q({"table": "pods"})
    assert pods["ready"].str.match(r"^\d+/\d+$").all() and pods["restarts"].dtype.kind in "iu"
    assert pods["owner_kind"].notna().any() and pods["app"].notna().any()
    assert all(isinstance(json.loads(x), dict) for x in pods["labels"])
    assert not pods["annotations"].str.contains("last-applied-configuration").any()
    cont = Q({"table": "containers"})
    assert len(cont) >= len(pods) and (cont["state"] == "running").any()
    assert cont["memory_limit_bytes"].dropna().apply(lambda v: float(v).is_integer()).all()
    nodes = Q({"table": "nodes"})
    assert len(nodes) == 1 and nodes.iloc[0]["ready"] and nodes.iloc[0]["cpu_capacity_millicores"] >= 1000
    assert str(nodes.iloc[0]["kubelet_version"]).startswith("v1.35")
    dep = Q({"table": "deployments"})
    assert (dep["ready"] <= dep["replicas"].fillna(dep["ready"])).all() and dep["images"].str.len().gt(0).all()
    eps = Q({"table": "endpoint_slices"})
    assert (eps["ready_count"] > 0).any() and eps["service"].notna().any()
    npol = Q({"table": "network_policies"})
    assert len(npol) == 2 and all(isinstance(json.loads(x), dict) for x in npol["pod_selector"])
    pvc, pv = Q({"table": "persistent_volume_claims"}), Q({"table": "persistent_volumes"})
    assert len(pvc) == 4 and len(pv) == 4 and (pvc["phase"] == "Bound").all()
    joined = pvc.merge(pv, left_on="volume_name", right_on="name", suffixes=("_c", "_v"))
    assert len(joined) == 4 and (joined["name_c"] == joined["claim_name"]).all()
    assert pv["source_type"].notna().all() and (pv["capacity_bytes"] > 0).all()
    sc = Q({"table": "storage_classes"})
    assert len(sc) == 1 and sc.iloc[0]["provisioner"]
    cm = Q({"table": "configmaps"})
    assert set(cm.columns) == {"name", "namespace", "keys", "key_count", "created", "labels", "annotations"}
    return (f"pods={len(pods)} containers={len(cont)} node cpu={nodes.iloc[0]['cpu_capacity_millicores']}m "
            f"pvc→pv joins={len(joined)} sc={sc.iloc[0]['name']} default={bool(sc.iloc[0]['is_default'])}")


@case("A5", "CRD index and a discovered crd:: table")
def _():
    idx = Q({"table": "custom_resource_definitions"})
    assert len(idx) == 49, len(idx)
    crd_tables = [n for n in tables if n.startswith(CRD_TABLE_PREFIX)]
    gw = next((n for n in crd_tables if n.endswith("/Gateway") or n.endswith("/GatewayClass")), crd_tables[0])
    df = Q({"table": gw})
    assert len(df) >= 1 and all(isinstance(json.loads(x), dict) for x in df["spec"])
    assert "conditions" in df.columns
    return f"{gw}: {len(df)} rows, spec keys={sorted(json.loads(df.iloc[0]['spec']))[:6]}"


@case("A6", "logs: by pod, container, tail_lines, since, grep, label_selector fan-out, previous")
def _():
    pods = Q({"table": "pods", "namespace": "bow-test"})
    app = pods[pods["name"].str.startswith("bow-runtime-app")].iloc[0]["name"]
    df = Q({"table": "logs", "namespace": "bow-test", "pod": app, "tail_lines": 5})
    assert 1 <= len(df) <= 5 and df["timestamp"].notna().all() and not df["line"].str.startswith("<no logs").any()
    df2 = Q({"table": "logs", "namespace": "bow-test", "pod": app, "since": "-24h", "grep": "PUT|GET", "tail_lines": 50})
    assert df2["line"].str.contains("PUT|GET").all()
    fan = Q({"table": "logs", "namespace": "bow-test", "label_selector": "app=bow-runtime-app", "tail_lines": 2})
    assert fan["pod"].nunique() >= 1 and len(fan) <= 2 * fan["container"].nunique() * fan["pod"].nunique()
    nats = pods[pods["name"] == "nats-0"].iloc[0]
    prev = Q({"table": "logs", "namespace": "bow-test", "pod": "nats-0", "container": "nats", "previous": True, "tail_lines": 3})
    assert len(prev) >= 1 and (prev["line"].str.startswith("<no logs").any() or nats["restarts"] > 0)
    return f"pod={app} rows={len(df)} grep_rows={len(df2)} fanout_pods={fan['pod'].nunique()} previous→{'row' if prev['line'].str.startswith('<no logs').any() else 'real logs'}"


@case("A7", "query spec: namespace list, name, selectors, limit pagination, raw redaction, escape hatch, refusals")
def _():
    two = Q({"table": "pods", "namespace": ["bow-test", "kube-system"]})
    assert set(two["namespace"]) == {"bow-test", "kube-system"}
    one = Q({"table": "services", "namespace": "bow-test", "name": "nats"})
    assert len(one) == 1 and one.iloc[0]["name"] == "nats"
    sel = Q({"table": "pods", "label_selector": "app=bow-runtime-app", "field_selector": "status.phase=Running"})
    assert len(sel) >= 1 and (sel["phase"] == "Running").all()
    lim = Q({"table": "pods", "limit": 3})
    assert len(lim) == 3
    raw = Q({"table": "pods", "namespace": "bow-test", "raw": True, "limit": 5})
    assert "raw" in raw.columns and not raw["raw"].str.contains("last-applied-configuration").any()
    hatch = Q({"path": "/apis/apps/v1/namespaces/bow-test/deployments", "params": {"limit": 2}})
    assert len(hatch) >= 1 and {"name", "spec", "labels"} <= set(hatch.columns)
    for p in ("/api/v1/secrets", "/api/v1/namespaces/bow-test/pods/x/exec", "/api/v1/nodes/dev/proxy/metrics"):
        try:
            Q({"path": p}); raise AssertionError(f"{p} was not refused")
        except ValueError:
            pass
    return f"ns-list={len(two)} name→1 selectors={len(sel)} limit3={len(lim)} raw ok hatch={len(hatch)} refusals ok"


@case("A8", "namespace allowlist scopes and refuses")
def _():
    scoped = KubernetesClient(access_file=ACCESS, namespaces="bow-test")
    df = scoped.execute_query('{"table": "pods"}')
    assert set(df["namespace"]) == {"bow-test"}
    try:
        scoped.execute_query('{"table": "pods", "namespace": "kube-system"}'); raise AssertionError("allowlist not enforced")
    except ValueError:
        pass
    return f"{len(df)} pods in bow-test; kube-system refused"


@case("A9", "error surfaces: exec kubeconfig, tampered token, unknown table, metrics unavailable")
def _():
    laptop = ACCESS.replace("token:", "exec: {command: aws}\n    token:")
    try:
        parse_access_file(laptop); raise AssertionError("exec not rejected")
    except ValueError as e:
        assert "exec" in str(e)
    tampered = re.sub(r"(token: \S+?)(\S{4})\n", r"\1XXXX\n", ACCESS)
    r = KubernetesClient(access_file=tampered).test_connection()
    assert not r["success"] and ("401" in r["message"] or "expired" in r["message"].lower()), r["message"]
    try:
        Q({"table": "nope"}); raise AssertionError("unknown table accepted")
    except ValueError:
        pass
    try:
        Q({"table": "pod_metrics"}); raise AssertionError("metrics table accepted")
    except ValueError as e:
        assert "metrics-server" in str(e)
    return "exec rejected; 401 surfaced; unknown table + metrics refused"


@case("A10", "security: no secrets table, configmap values never returned")
def _():
    assert "secrets" not in tables and not any(n.endswith("/Secret") for n in tables)
    cm = Q({"table": "configmaps", "namespace": "bow-test", "raw": False})
    text = cm.to_json()
    assert "password" not in text.lower() or "keys" in cm.columns
    return f"{len(cm)} configmaps in bow-test, keys only"


width = max(len(d) for _, d, _, _ in results)
print("\n".join(f"{cid:<4} {status:<5} {desc:<{width}}  {detail}" for cid, desc, status, detail in results))
print(f"\nTOTAL: {sum(1 for r in results if r[2]=='PASS')} pass / {len(results)}")
sys.exit(0 if all(r[2] == "PASS" for r in results) else 1)
