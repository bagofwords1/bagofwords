"""Unit tests for KubernetesClient.

Covers:
- parse_access_file: accepts exactly the shape print_access_file.sh prints and
  rejects every other kubeconfig flavour (exec plugins, client certs, auth
  providers, several clusters/users/contexts, missing CA, non-JWT token) with
  a message naming the stanza
- quantity / duration / timestamp parsing across every legal form
- get_schemas(): the fixed catalog, uniform labels/annotations on object-
  backed tables only, the storage + networking + ownership foreign-key
  chains, metrics tables present iff metrics.k8s.io is served, populated-only
  CRD discovery with the max_crd_tables cap and progress reporting
- execute_query(): row builders (pods ready/restarts/owner/app, per-container
  state/reason/exit codes, PV/PVC/StorageClass chain, ingress rule
  flattening, endpoint-slice readiness, events `since`), server-side
  selectors passed through, namespace allowlist enforcement, single-object
  fetch, `raw` redaction, logs fan-out / caps / previous / grep, metrics rows,
  the escape hatch and what it refuses
- test_connection(): counts on success, the not-cluster-wide 403, expired 401,
  TLS/transport failures
- registry: entry, single system-scoped auth variant, three-step setup guide,
  the guide's script pinned to tools/kubernetes/print_access_file.sh

The kubernetes `ApiClient` (the HTTP boundary) is faked and replays JSON
captured from the seeded kind sandbox (tests/unit/fixtures/kubernetes/), so
these run without a cluster.
"""
from __future__ import annotations

import base64
import json
import pathlib
import re
from urllib.parse import parse_qs

import pandas as pd
import pytest

from app.data_sources.clients import kubernetes_client as kc
from app.data_sources.clients.kubernetes_client import (
    KubernetesClient,
    _CATALOG,
    _RUNTIME_TABLES,
    clean_annotations,
    cpu_millicores,
    parse_access_file,
    parse_duration_seconds,
    parse_quantity,
    parse_rfc3339,
    redact_object,
    to_bytes,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "kubernetes"

JWT = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYyJ9.eyJpc3MiOiJrdWJlcm5ldGVzL3NlcnZpY2VhY2NvdW50In0.c2lnbmF0dXJl"
CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUdGVzdA==\n-----END CERTIFICATE-----\n"
CA_B64 = base64.b64encode(CA_PEM.encode()).decode()


def access_file(server="https://10.0.0.1:6443", token=JWT, ca=CA_B64, user_extra=None, cluster_extra=None,
                contexts=True):
    cluster = {"server": server}
    if ca is not None:
        cluster["certificate-authority-data"] = ca
    cluster.update(cluster_extra or {})
    user = {"token": token} if token is not None else {}
    user.update(user_extra or {})
    doc = {"apiVersion": "v1", "kind": "Config",
           "clusters": [{"name": "bagofwords", "cluster": cluster}],
           "users": [{"name": "bagofwords-reader", "user": user}]}
    if contexts:
        doc["contexts"] = [{"name": "bagofwords", "context": {"cluster": "bagofwords", "user": "bagofwords-reader"}}]
        doc["current-context"] = "bagofwords"
    return json.dumps(doc)  # JSON is YAML — the parser must accept both


# ── the fake HTTP boundary ─────────────────────────────────────────────────────


class _FakeResponse:
    """The subset of urllib3's HTTPResponse that ApiClient/ApiException read."""

    def __init__(self, data: bytes, status: int = 200, reason: str = "OK"):
        self.data = data
        self.status = status
        self.reason = reason

    def getheaders(self):
        return {}


class _FakeApiClient:
    """Replays fixtures by API path and records every call.

    Fixtures are the `-A` (all-namespaces) lists captured from the sandbox;
    namespaced paths are filtered from them and named gets resolved against
    them, so one capture per kind serves every path shape the client builds.
    """

    calls: list = []           # (path, params_dict, accept) — reset per instance below
    fixtures_dir = FIXTURES
    overrides: dict = {}       # path -> payload | ApiException | callable(params)->payload
    metrics_served = True

    def __init__(self, configuration=None, *a, **kw):
        self.configuration = configuration
        type(self).calls = []

    def close(self):
        pass

    # -- fixture lookup --
    @classmethod
    def _load(cls, name: str):
        p = cls.fixtures_dir / name
        if not p.exists():
            return None
        if p.suffix == ".json":
            return json.loads(p.read_text())
        return p.read_text()

    @classmethod
    def _list_fixture(cls, api: str, plural: str):
        return cls._load(f"{api.strip('/').replace('/', '_')}_{plural}.json")

    @staticmethod
    def _match_selector(item: dict, selector: str | None, field: bool) -> bool:
        if not selector:
            return True
        for clause in selector.split(","):
            if "!=" in clause:
                k, v = clause.split("!=", 1)
                neg = True
            elif "=" in clause:
                k, v = clause.split("==", 1) if "==" in clause else clause.split("=", 1)
                neg = False
            else:
                continue
            if field:
                cur = item
                for part in k.split("."):
                    cur = (cur or {}).get(part) if isinstance(cur, dict) else None
                actual = cur
            else:
                actual = ((item.get("metadata") or {}).get("labels") or {}).get(k)
            if (str(actual) == v) == neg:
                return False
        return True

    def call_api(self, resource_path, method, path_params=None, query_params=None, header_params=None, **kw):
        from kubernetes.client.rest import ApiException

        params = {k: v for k, v in (query_params or [])}
        accept = (header_params or {}).get("Accept", "application/json")
        type(self).calls.append((resource_path, params, accept))
        assert method == "GET", "the connector must never issue anything but GET"

        override = self.overrides.get(resource_path)
        if override is not None:
            if isinstance(override, Exception):
                raise override
            payload = override(params) if callable(override) else override
            return self._encode(payload)

        if resource_path == "/version":
            return self._encode(self._load("version.json") or {"gitVersion": "v1.31.0", "major": "1", "minor": "31"})
        if resource_path == kc.METRICS_GROUP_PATH:
            if not self.metrics_served:
                raise ApiException(status=404, reason="Not Found")
            return self._encode({"kind": "APIResourceList", "resources": [{"name": "pods"}, {"name": "nodes"}]})

        # /<api...>/namespaces/<ns>/pods/<name>/log
        m = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/log$", resource_path)
        if m:
            text = self._load(f"log_{m.group(1)}_{m.group(2)}_{params.get('container') or 'default'}.txt")
            if text is None:
                raise ApiException(status=400, reason="Bad Request",
                                   http_resp=_FakeResponse(json.dumps({"message": "previous terminated container not found"}).encode(), 400))
            return _FakeResponse(text.encode())

        # /<api>/[namespaces/<ns>/]<plural>[/<name>]
        m = re.match(r"^(/api/v1|/apis/[^/]+/[^/]+)(?:/namespaces/([^/]+))?/([^/]+)(?:/([^/]+))?$", resource_path)
        if not m:
            raise ApiException(status=404, reason="Not Found",
                               http_resp=_FakeResponse(json.dumps({"message": "the server could not find the requested resource"}).encode(), 404))
        api, ns, plural, name = m.groups()
        fixture = self._list_fixture(api, plural)
        if fixture is None:
            raise ApiException(status=404, reason="Not Found",
                               http_resp=_FakeResponse(json.dumps({"message": "the server could not find the requested resource"}).encode(), 404))
        items = list(fixture.get("items") or [])
        if ns:
            items = [i for i in items if (i.get("metadata") or {}).get("namespace") == ns]
        items = [i for i in items if self._match_selector(i, params.get("labelSelector"), field=False)]
        items = [i for i in items if self._match_selector(i, params.get("fieldSelector"), field=True)]
        if name:
            for i in items:
                if (i.get("metadata") or {}).get("name") == name:
                    return self._encode(i)
            raise ApiException(status=404, reason="Not Found",
                               http_resp=_FakeResponse(json.dumps({"message": f'{plural} "{name}" not found'}).encode(), 404))
        # server-side pagination: honour limit/continue
        limit = int(params.get("limit") or 0) or len(items)
        start = int(params.get("continue") or 0)
        page = items[start:start + limit]
        meta = {}
        if start + limit < len(items):
            meta["continue"] = str(start + limit)
        return self._encode({"kind": fixture.get("kind"), "items": page, "metadata": meta})

    @staticmethod
    def _encode(payload):
        return _FakeResponse(json.dumps(payload, default=str).encode())


@pytest.fixture
def fake_api(monkeypatch):
    """Install the fake ApiClient; returns the class so tests can tweak it."""
    _FakeApiClient.overrides = {}
    _FakeApiClient.metrics_served = True
    monkeypatch.setattr(kc, "ApiClient", _FakeApiClient)
    return _FakeApiClient


def make_client(**kw) -> KubernetesClient:
    return KubernetesClient(access_file=access_file(), **kw)


def seeded_pod(app: str) -> str:
    """Name of the seeded pod carrying label app=<app> (pod names carry a
    generated hash, so tests look them up instead of hard-coding them)."""
    pods = json.loads((FIXTURES / "api_v1_pods.json").read_text())["items"]
    return next(p["metadata"]["name"] for p in pods if (p["metadata"].get("labels") or {}).get("app") == app)


def api_error(status: int, message: str):
    from kubernetes.client.rest import ApiException
    return ApiException(status=status, reason="x", http_resp=_FakeResponse(json.dumps({"message": message}).encode(), status))


# ── access file ───────────────────────────────────────────────────────────────


class TestParseAccessFile:
    def test_script_shape_accepted(self):
        server, ca, token = parse_access_file(access_file())
        assert server == "https://10.0.0.1:6443"
        assert ca == CA_PEM
        assert token == JWT

    def test_contexts_optional(self):
        server, ca, token = parse_access_file(access_file(contexts=False))
        assert (server, token) == ("https://10.0.0.1:6443", JWT)

    def test_yaml_and_json_both_accepted(self):
        doc = json.loads(access_file())
        import yaml
        assert parse_access_file(yaml.safe_dump(doc))[2] == JWT

    def test_trailing_slash_stripped(self):
        assert parse_access_file(access_file(server="https://api.example.com/"))[0] == "https://api.example.com"

    def test_http_server_without_ca_allowed(self):
        server, ca, _ = parse_access_file(access_file(server="http://127.0.0.1:8001", ca=None))
        assert server.startswith("http://") and ca is None

    @pytest.mark.parametrize("stanza,value", [
        ("exec", {"command": "aws", "args": ["eks", "get-token"]}),
        ("auth-provider", {"name": "gcp"}),
        ("client-certificate-data", "abc"),
        ("client-certificate", "/home/me/.kube/cert.pem"),
        ("client-key-data", "abc"),
        ("username", "admin"),
    ])
    def test_rejects_other_user_stanzas_by_name(self, stanza, value):
        with pytest.raises(ValueError) as e:
            parse_access_file(access_file(user_extra={stanza: value}))
        assert stanza in str(e.value)

    def test_exec_plugin_reported_even_with_a_bad_ca(self):
        # A laptop kubeconfig usually fails BOTH checks; the exec plugin is the
        # reason the admin needs to hear, so it must win.
        with pytest.raises(ValueError) as e:
            parse_access_file(access_file(ca="LS0tLS1CRUdJTi0tLS0t", user_extra={"exec": {"command": "aws"}}))
        assert "exec" in str(e.value)

    def test_rejects_insecure_skip_tls_verify(self):
        with pytest.raises(ValueError) as e:
            parse_access_file(access_file(cluster_extra={"insecure-skip-tls-verify": True}))
        assert "insecure-skip-tls-verify" in str(e.value)

    def test_rejects_ca_file_reference(self):
        with pytest.raises(ValueError) as e:
            parse_access_file(access_file(ca=None, cluster_extra={"certificate-authority": "/etc/ca.pem"}))
        assert "certificate-authority" in str(e.value)

    def test_https_requires_embedded_ca(self):
        with pytest.raises(ValueError) as e:
            parse_access_file(access_file(ca=None))
        assert "certificate-authority-data" in str(e.value)

    def test_rejects_bad_base64_and_non_pem_ca(self):
        with pytest.raises(ValueError):
            parse_access_file(access_file(ca="not*base64"))
        with pytest.raises(ValueError):
            parse_access_file(access_file(ca=base64.b64encode(b"hello").decode()))

    def test_rejects_multiple_clusters_users_contexts(self):
        doc = json.loads(access_file())
        two_clusters = dict(doc, clusters=doc["clusters"] * 2)
        two_users = dict(doc, users=doc["users"] * 2)
        two_ctx = dict(doc, contexts=doc["contexts"] * 2)
        for bad in (two_clusters, two_users, two_ctx):
            with pytest.raises(ValueError):
                parse_access_file(json.dumps(bad))

    def test_rejects_non_jwt_token(self):
        with pytest.raises(ValueError) as e:
            parse_access_file(access_file(token="k8s-aws-v1.aGVsbG8"))
        assert "token" in str(e.value)

    def test_rejects_missing_token_and_server(self):
        with pytest.raises(ValueError):
            parse_access_file(access_file(token=None))
        with pytest.raises(ValueError):
            parse_access_file(access_file(server=""))

    @pytest.mark.parametrize("text", ["", "   ", "just a string", "- a\n- b", "clusters: [\n"])
    def test_rejects_non_document_input(self, text):
        with pytest.raises(ValueError):
            parse_access_file(text)


# ── parsers ───────────────────────────────────────────────────────────────────


class TestParsers:
    @pytest.mark.parametrize("q,expected", [
        ("250m", 250), ("1", 1000), ("0.5", 500), ("2", 2000), ("1500m", 1500), ("100n", 0), ("1e3", 1_000_000),
    ])
    def test_cpu_millicores(self, q, expected):
        assert cpu_millicores(q) == expected

    @pytest.mark.parametrize("q,expected", [
        ("512Mi", 512 * 2**20), ("1Gi", 2**30), ("1000000000", 10**9), ("1e9", 10**9), ("2G", 2 * 10**9),
        ("128974848", 128974848), ("50Gi", 50 * 2**30), (1024, 1024),
    ])
    def test_to_bytes(self, q, expected):
        assert to_bytes(q) == expected

    @pytest.mark.parametrize("q", [None, "", "abc", "1Xi", "12 13"])
    def test_unparseable_quantity_is_none(self, q):
        assert parse_quantity(q) is None

    @pytest.mark.parametrize("v,expected", [
        ("-1h", 3600), ("-15m", 900), ("24h", 86400), ("7d", 7 * 86400), ("1w", 604800), (900, 900), ("900", 900),
        ("30.017s", 30), ("1m30s", 90), ("15s", 15),
    ])
    def test_duration_seconds(self, v, expected):
        assert parse_duration_seconds(v) == expected

    def test_rfc3339_variants_are_utc(self):
        for s in ("2026-09-06T10:00:00Z", "2026-09-06T10:00:00.123456Z", "2026-09-06T12:00:00+02:00"):
            ts = parse_rfc3339(s)
            assert isinstance(ts, pd.Timestamp) and str(ts.tz) == "UTC"
            assert ts.hour == 10
        assert parse_rfc3339(None) is None and parse_rfc3339("nope") is None

    def test_clean_annotations_drops_last_applied_and_caps(self):
        ann = {"kubectl.kubernetes.io/last-applied-configuration": "{" + "x" * 10_000 + "}",
               "deployment.kubernetes.io/revision": "3",
               "big": "y" * 10_000}
        out = clean_annotations(ann)
        assert "kubectl.kubernetes.io/last-applied-configuration" not in out
        assert out["deployment.kubernetes.io/revision"] == "3"
        assert len(json.dumps(out)) <= kc.ANNOTATION_CAP_BYTES + 64
        assert out["big"].endswith("…")

    def test_redact_object_scrubs_env_literals_everywhere(self):
        pod_spec = {"containers": [{"name": "c", "env": [{"name": "SECRET", "value": "hunter2"},
                                                         {"name": "REF", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]}],
                    "initContainers": [{"name": "i", "env": [{"name": "X", "value": "1"}]}]}
        for obj in (
            {"kind": "Pod", "spec": pod_spec},
            {"kind": "Deployment", "spec": {"template": {"spec": pod_spec}}},
            {"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"template": {"spec": pod_spec}}}}},
        ):
            obj["metadata"] = {"annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}", "keep": "1"}}
            text = json.dumps(redact_object(obj))
            assert "hunter2" not in text and '"value": "1"' not in text
            assert "secretKeyRef" in text                     # refs are kept
            assert "last-applied-configuration" not in text and '"keep": "1"' in text


# ── catalog ───────────────────────────────────────────────────────────────────


class TestCatalog:
    def test_all_fixed_and_runtime_tables_present(self, fake_api):
        names = {t.name for t in make_client().get_schemas()}
        assert set(_CATALOG) <= names
        assert {"pod_metrics", "node_metrics", "logs"} <= names
        assert not any(n == "secrets" or n.endswith("/Secret") for n in names)

    def test_metrics_tables_only_when_served(self, fake_api):
        fake_api.metrics_served = False
        tables = {t.name: t for t in make_client().get_schemas()}
        assert "pod_metrics" not in tables and "node_metrics" not in tables
        assert "logs" in tables
        assert "metrics-server" in (tables["nodes"].description or "")

    def test_metadata_columns_uniform_on_object_tables_only(self, fake_api):
        tables = {t.name: t for t in make_client().get_schemas()}
        derived = {"containers", "logs", "pod_metrics", "node_metrics"}
        for name, t in tables.items():
            cols = {c.name for c in t.columns}
            if name in derived:
                assert not {"labels", "annotations"} & cols, name
            else:
                assert {"labels", "annotations"} <= cols, name

    def test_promoted_label_columns(self, fake_api):
        tables = {t.name: t for t in make_client().get_schemas()}
        assert {"zone", "region", "instance_type"} <= {c.name for c in tables["nodes"].columns}
        assert "app" in {c.name for c in tables["pods"].columns}

    def test_foreign_key_chains(self, fake_api):
        tables = {t.name: t for t in make_client().get_schemas()}

        def fks(name):
            return {(f.column.name, f.references_name) for f in tables[name].fks}

        assert ("volume_name", "persistent_volumes") in fks("persistent_volume_claims")
        assert ("storage_class", "storage_classes") in fks("persistent_volumes")
        assert ("csi_driver", "csi_drivers") in fks("persistent_volumes")
        assert ("provisioner", "csi_drivers") in fks("storage_classes")
        assert ("ingress_class", "ingress_classes") in fks("ingresses")
        assert ("service", "services") in fks("endpoint_slices")
        assert ("node", "nodes") in fks("pods")
        assert ("owner_name", "deployments") in fks("replicasets")
        assert ("owner_name", "cronjobs") in fks("jobs")
        assert ("pod", "pods") in fks("containers")

    def test_crd_tables_only_for_populated_kinds(self, fake_api):
        tables = {t.name: t for t in make_client().get_schemas()}
        crd_tables = [n for n in tables if n.startswith(kc.CRD_TABLE_PREFIX)]
        assert "crd::example.com/Widget" in crd_tables
        # every discovered table has objects; no internal-group table ever appears
        for n in crd_tables:
            assert not any(g in n for g in kc._INTERNAL_CRD_GROUPS)
        desc = tables["crd::example.com/Widget"].description or ""
        assert "size" in desc and "replicas" in desc          # top-level spec keys advertised
        assert "namespace" in {c.name for c in tables["crd::example.com/Widget"].columns}

    def test_crd_discovery_respects_cap_and_lists_the_rest(self, fake_api):
        # A second populated CRD so the cap has something to cut.
        crds = json.loads((FIXTURES / "apis_apiextensions.k8s.io_v1_customresourcedefinitions.json").read_text())
        gadget = json.loads(json.dumps(next(c for c in crds["items"] if c["spec"]["group"] == "example.com")))
        gadget["metadata"]["name"] = "gadgets.example.com"
        gadget["spec"]["names"] = {"plural": "gadgets", "singular": "gadget", "kind": "Gadget"}
        fake_api.overrides["/apis/apiextensions.k8s.io/v1/customresourcedefinitions"] = {"items": crds["items"] + [gadget], "metadata": {}}
        fake_api.overrides["/apis/example.com/v1/gadgets"] = {"items": [{"metadata": {"name": "g1", "namespace": "payments"}, "spec": {"color": "red"}}], "metadata": {}}

        tables = {t.name: t for t in make_client(max_crd_tables=1).get_schemas()}
        crd_tables = [n for n in tables if n.startswith(kc.CRD_TABLE_PREFIX)]
        assert len(crd_tables) == 1
        skipped = ({"crd::example.com/Gadget", "crd::example.com/Widget"} - set(crd_tables)).pop()
        assert skipped in (tables["custom_resource_definitions"].description or "")
        # …and the skipped kind is still queryable by name
        assert len(make_client(max_crd_tables=1).execute_query(json.dumps({"table": skipped}))) >= 1
        assert not [t for t in make_client(discover_crds=False).get_schemas() if t.name.startswith(kc.CRD_TABLE_PREFIX)]
        assert not [t for t in make_client(max_crd_tables=0).get_schemas() if t.name.startswith(kc.CRD_TABLE_PREFIX)]

    def test_discovery_reports_progress_and_survives_crd_errors(self, fake_api):
        seen = []
        make_client().get_schemas(progress_callback=lambda phase, item, done, total: seen.append((phase, item)))
        assert seen and all(p == "custom resources" for p, _ in seen)
        fake_api.overrides["/apis/apiextensions.k8s.io/v1/customresourcedefinitions"] = api_error(403, "forbidden")
        names = {t.name for t in make_client().get_schemas()}
        assert set(_CATALOG) <= names                       # the fixed catalog never depends on CRD access

    def test_get_schema_for_every_kind_of_table(self, fake_api):
        c = make_client()
        assert c.get_schema("pods").name == "pods"
        assert c.get_schema("logs").name == "logs"
        assert c.get_schema("crd::example.com/Widget").pks[0].name == "name"
        with pytest.raises(ValueError):
            c.get_schema("crd::example.com/Nope")
        with pytest.raises(ValueError):
            c.get_schema("secrets")

    def test_prompt_schema_renders(self, fake_api):
        text = make_client().prompt_schema()
        assert "table: pods" in text and "table: persistent_volumes" in text


# ── queries ───────────────────────────────────────────────────────────────────


class TestPods:
    def test_pod_rows_ready_restarts_owner_app(self, fake_api):
        df = make_client().execute_query('{"table": "pods", "namespace": "payments"}')
        assert set(df["namespace"]) == {"payments"}
        assert {"ready", "restarts", "owner_kind", "owner_name", "app", "labels", "annotations"} <= set(df.columns)
        assert df["ready"].str.match(r"^\d+/\d+$").all()
        crash = df[df["app"] == "fraud-scorer"]
        assert len(crash) == 1 and crash.iloc[0]["owner_kind"] == "ReplicaSet" and crash.iloc[0]["restarts"] >= 1
        assert (df["owner_kind"].isna() | df["owner_kind"].isin(["ReplicaSet", "StatefulSet", "DaemonSet", "Job"])).all()

    def test_labels_are_json_and_last_applied_is_stripped(self, fake_api):
        df = make_client().execute_query('{"table": "deployments", "namespace": "payments", "name": "checkout"}')
        labels = json.loads(df.iloc[0]["labels"])
        assert labels.get("app.kubernetes.io/name") == "checkout"
        assert "last-applied-configuration" not in df.iloc[0]["annotations"]

    def test_raw_column_redacts_env_and_last_applied(self, fake_api):
        df = make_client().execute_query('{"table": "pods", "namespace": "payments", "label_selector": "app=checkout", "raw": true}')
        raw = df.iloc[0]["raw"]
        assert "sk-live-should-be-redacted" not in raw and kc.ENV_REDACTED in raw
        assert "last-applied-configuration" not in raw
        assert "raw" not in make_client().execute_query('{"table": "pods", "limit": 1}').columns

    def test_containers_carry_state_reason_and_normalized_resources(self, fake_api):
        df = make_client().execute_query('{"table": "containers", "namespace": "payments"}')
        failing = df[df["reason"].isin(["CrashLoopBackOff", "OOMKilled", "Error"]) | df["last_state_reason"].isin(["OOMKilled", "Error"])]
        assert len(failing) >= 2                                   # the crash-looper and the OOM-killer
        scorer = df[df["pod"].str.startswith("fraud-scorer")].iloc[0]
        assert scorer["cpu_limit_millicores"] == 100 and scorer["memory_limit_bytes"] == 32 * 2**20
        assert scorer["last_exit_code"] == 1
        assert "labels" not in df.columns

    def test_selectors_are_passed_server_side(self, fake_api):
        make_client().execute_query('{"table": "pods", "namespace": "payments", "label_selector": "app=checkout", "field_selector": "status.phase=Running"}')
        path, params, _ = fake_api.calls[-1]
        assert path == "/api/v1/namespaces/payments/pods"
        assert params["labelSelector"] == "app=checkout" and params["fieldSelector"] == "status.phase=Running"

    def test_all_namespaces_is_one_call_and_limit_paginates(self, fake_api):
        c = make_client()
        df = c.execute_query('{"table": "pods", "limit": 3}')
        assert len(df) == 3
        paths = [p for p, _, _ in fake_api.calls if p.endswith("/pods")]
        assert paths == ["/api/v1/pods"]
        big = c.execute_query('{"table": "pods"}')
        assert len(big) > 3 and len({p for p, _, _ in fake_api.calls}) == 1

    def test_namespace_allowlist_scopes_and_refuses(self, fake_api):
        c = make_client(namespaces="payments, inventory")
        df = c.execute_query('{"table": "pods"}')
        assert set(df["namespace"]) <= {"payments", "inventory"}
        assert {p for p, _, _ in fake_api.calls} == {"/api/v1/namespaces/payments/pods", "/api/v1/namespaces/inventory/pods"}
        with pytest.raises(ValueError):
            c.execute_query('{"table": "pods", "namespace": "kube-system"}')

    def test_single_object_fetch_paths(self, fake_api):
        c = make_client()
        df = c.execute_query('{"table": "nodes", "name": "bow-k8s-worker"}')
        assert len(df) == 1 and fake_api.calls[-1][0] == "/api/v1/nodes/bow-k8s-worker"
        c.execute_query('{"table": "deployments", "namespace": "payments", "name": "checkout"}')
        assert fake_api.calls[-1][0] == "/apis/apps/v1/namespaces/payments/deployments/checkout"
        with pytest.raises(RuntimeError) as e:
            c.execute_query('{"table": "deployments", "namespace": "payments", "name": "missing"}')
        assert "not found" in str(e.value).lower()

    def test_cluster_scoped_kinds_ignore_namespace(self, fake_api):
        make_client().execute_query('{"table": "storage_classes", "namespace": "payments"}')
        assert fake_api.calls[-1][0] == "/apis/storage.k8s.io/v1/storageclasses"


class TestOtherTables:
    def test_nodes_have_capacity_and_topology(self, fake_api):
        df = make_client().execute_query('{"table": "nodes"}')
        assert len(df) >= 2 and df["ready"].all()
        assert (df["cpu_capacity_millicores"] > 0).all() and (df["memory_allocatable_bytes"] > 0).all()
        assert df[df["name"].str.contains("control-plane")].iloc[0]["roles"] == "control-plane"

    def test_storage_chain_columns(self, fake_api):
        c = make_client()
        pvc = c.execute_query('{"table": "persistent_volume_claims"}')
        pending = pvc[pvc["phase"] == "Pending"].iloc[0]
        assert pending["storage_class"] == "slow-nfs" and pending["requested_bytes"] == 50 * 2**30
        bound = pvc[pvc["phase"] == "Bound"].iloc[0]
        pv = c.execute_query('{"table": "persistent_volumes"}')
        vol = pv[pv["name"] == bound["volume_name"]].iloc[0]
        assert vol["claim_name"] == bound["name"] and vol["phase"] == "Bound"
        assert vol["source_type"] in ("hostPath", "local", "csi") and vol["capacity_bytes"] == 2**30
        sc = c.execute_query('{"table": "storage_classes"}')
        assert sc[sc["name"] == "slow-nfs"].iloc[0]["provisioner"] == "nfs.example.com/provisioner"
        assert sc["is_default"].sum() == 1
        assert isinstance(sc.iloc[0]["parameters"], str)

    def test_networking_rows(self, fake_api):
        c = make_client()
        ing = c.execute_query('{"table": "ingresses", "namespace": "payments"}').iloc[0]
        assert ing["ingress_class"] == "public-nginx" and "shop.example.com" in ing["hosts"]
        rules = json.loads(ing["rules"])
        assert rules[0]["service"] == "checkout" and rules[0]["port"] == 80 and rules[0]["path"] == "/checkout"
        assert "shop.example.com" in ing["tls_hosts"]
        classes = c.execute_query('{"table": "ingress_classes"}')
        assert "public-nginx" not in set(classes.get("name", pd.Series(dtype=str)))
        eps = c.execute_query('{"table": "endpoint_slices", "namespace": "payments", "label_selector": "kubernetes.io/service-name=checkout"}')
        assert eps.iloc[0]["service"] == "checkout" and eps.iloc[0]["ready_count"] == 2
        np_ = c.execute_query('{"table": "network_policies", "namespace": "payments"}').iloc[0]
        assert np_["policy_types"] == "Ingress" and json.loads(np_["ingress_rules"])
        svc = c.execute_query('{"table": "services", "namespace": "payments", "name": "checkout"}').iloc[0]
        assert json.loads(svc["selector"]) == {"app": "checkout"} and json.loads(svc["ports"])[0]["port"] == 80

    def test_workload_rows(self, fake_api):
        c = make_client()
        dep = c.execute_query('{"table": "deployments", "namespace": "payments"}').set_index("name")
        assert dep.loc["checkout", "ready"] == 2 and dep.loc["fraud-scorer", "unavailable"] >= 1
        assert "nginx" in dep.loc["checkout", "images"]
        rs = c.execute_query('{"table": "replicasets", "namespace": "payments", "label_selector": "app=checkout"}').iloc[0]
        assert rs["owner_name"] == "checkout" and rs["revision"] == "1"
        cj = c.execute_query('{"table": "cronjobs", "namespace": "payments"}').iloc[0]
        assert cj["schedule"] == "*/2 * * * *" and cj["concurrency_policy"] == "Forbid"
        jobs = c.execute_query('{"table": "jobs", "namespace": "payments"}')
        assert (jobs["owner_name"] == "nightly-reconcile").all() and (jobs["failed"] >= 1).any()
        hpa = c.execute_query('{"table": "horizontal_pod_autoscalers", "namespace": "payments"}').iloc[0]
        assert hpa["target_name"] == "checkout" and hpa["max_replicas"] == 5
        sts = c.execute_query('{"table": "statefulsets", "namespace": "inventory"}').iloc[0]
        assert sts["service_name"] == "catalog-db" and sts["ready"] == 1
        ds = c.execute_query('{"table": "daemonsets"}')
        assert (ds["desired"] >= 1).all()

    def test_configmaps_expose_keys_only(self, fake_api):
        df = make_client().execute_query('{"table": "configmaps", "namespace": "inventory", "name": "catalog-config"}')
        row = df.iloc[0]
        assert "DATABASE_URL" in row["keys"] and row["key_count"] == 2
        assert "hunter2" not in df.to_json()


    def test_resource_quota_and_namespaces(self, fake_api):
        c = make_client()
        rq = c.execute_query('{"table": "resource_quotas", "namespace": "inventory"}').iloc[0]
        assert json.loads(rq["hard"])["pods"] == "10"
        ns = c.execute_query('{"table": "namespaces"}')
        assert {"payments", "inventory"} <= set(ns["name"]) and json.loads(ns.set_index("name").loc["payments", "labels"])["team"] == "payments"

    def test_events_since_filters_client_side(self, fake_api):
        c = make_client()
        allev = c.execute_query('{"table": "events", "namespace": "payments", "limit": 5000}')
        assert {"reason", "involved_kind", "last_seen"} <= set(allev.columns) and len(allev) > 0
        recent = c.execute_query('{"table": "events", "namespace": "payments", "since": "-1s", "limit": 5000}')
        assert len(recent) <= len(allev)
        assert fake_api.calls[-1][0] == "/api/v1/namespaces/payments/events"

    def test_crd_table_rows(self, fake_api):
        df = make_client().execute_query('{"table": "crd::example.com/Widget"}')
        assert set(df["name"]) == {"blue-widget", "red-widget"}
        assert json.loads(df.set_index("name").loc["blue-widget", "spec"])["replicas"] == 3
        assert fake_api.calls[-1][0] == "/apis/example.com/v1/widgets"
        idx = make_client().execute_query('{"table": "custom_resource_definitions"}')
        assert idx.set_index("name").loc["widgets.example.com", "kind"] == "Widget"

    def test_metrics_rows_normalized(self, fake_api):
        c = make_client()
        pm = c.execute_query('{"table": "pod_metrics", "namespace": "payments"}')
        assert {"pod", "container", "cpu_millicores", "memory_bytes", "window_seconds"} <= set(pm.columns) and len(pm) > 0
        assert (pm["memory_bytes"] > 0).all()
        nm = c.execute_query('{"table": "node_metrics"}')
        assert len(nm) >= 2 and (nm["cpu_millicores"] >= 0).all()

    def test_metrics_unavailable_is_a_clear_error(self, fake_api):
        fake_api.metrics_served = False
        c = make_client()
        c.get_schemas()
        with pytest.raises(ValueError):
            c.execute_query('{"table": "pod_metrics"}')


class TestLogs:
    def test_requires_pod_or_selector(self, fake_api):
        with pytest.raises(ValueError):
            make_client().execute_query('{"table": "logs", "namespace": "payments"}')

    def test_single_pod_all_containers_and_params(self, fake_api):
        df = make_client(log_tail_default=200).execute_query(
            json.dumps({"table": "logs", "namespace": "payments", "pod": seeded_pod("fraud-scorer"), "previous": True, "since": "-30m"}))
        log_calls = [(p, q, a) for p, q, a in fake_api.calls if p.endswith("/log")]
        assert log_calls and all(q["timestamps"] == "true" and q["previous"] == "true" and q["sinceSeconds"] == 1800
                                 and q["tailLines"] == 200 for _, q, _ in log_calls)
        # A bare `text/plain` is answered with 406 by some API servers (microk8s
        # 1.35): the log request must always carry a wildcard fallback.
        assert all("*/*" in a for _, _, a in log_calls)
        assert {"timestamp", "line", "container"} <= set(df.columns) and len(df) > 0
        assert df["timestamp"].notna().any()

    def test_tail_cap_grep_and_container(self, fake_api):
        c = make_client(log_tail_max=100)           # below the 500 default → the cap wins
        df = c.execute_query(json.dumps({"table": "logs", "namespace": "payments", "pod": seeded_pod("fraud-scorer"), "container": "scorer", "tail_lines": 99999, "grep": "ERROR"}))
        _, q, _ = fake_api.calls[-1]
        assert q["tailLines"] == 100 and q["container"] == "scorer"
        assert df["line"].str.contains("ERROR").all()

    def test_selector_fans_out_capped(self, fake_api):
        c = make_client(max_log_pods=1)
        df = c.execute_query('{"table": "logs", "namespace": "payments", "label_selector": "app=checkout"}')
        assert df["pod"].nunique() == 1
        list_call = next(q for p, q, _ in fake_api.calls if p == "/api/v1/namespaces/payments/pods")
        assert list_call["labelSelector"] == "app=checkout" and list_call["limit"] == 1

    def test_missing_previous_run_becomes_a_row_not_an_error(self, fake_api):
        df = make_client().execute_query('{"table": "logs", "namespace": "payments", "pod": "gpu-batch"}')
        assert len(df) == 1 and df.iloc[0]["line"].startswith("<no logs")


class TestEscapeHatch:
    @pytest.mark.parametrize("path", [
        "/api/v1/secrets", "/api/v1/namespaces/payments/secrets/tls", "/api/v1/namespaces/x/pods/p/exec",
        "/api/v1/namespaces/x/pods/p/attach", "/api/v1/namespaces/x/pods/p/portforward",
        "/api/v1/nodes/n/proxy/metrics", "/api/v1/namespaces/x/services/s/proxy/", "api/v1/pods",
    ])
    def test_refused_paths(self, fake_api, path):
        with pytest.raises(ValueError):
            make_client().execute_query(json.dumps({"path": path}))
        assert not [p for p, _, _ in fake_api.calls if "secret" in p or "exec" in p or "proxy" in p]

    def test_list_path_returns_rows_with_metadata(self, fake_api):
        df = make_client().execute_query('{"path": "/apis/apps/v1/namespaces/payments/deployments", "params": {"labelSelector": "app.kubernetes.io/name=checkout"}}')
        assert list(df["name"]) == ["checkout"] and {"labels", "annotations", "spec", "status"} <= set(df.columns)
        assert fake_api.calls[-1][1]["labelSelector"] == "app.kubernetes.io/name=checkout"

    def test_non_list_path_returns_one_row(self, fake_api):
        df = make_client().execute_query('{"path": "/version"}')
        assert len(df) == 1 and "gitVersion" in df.columns


class TestSpecValidation:
    def test_bad_json_and_unknown_table(self, fake_api):
        c = make_client()
        with pytest.raises(ValueError):
            c.execute_query("SELECT * FROM pods")
        with pytest.raises(ValueError):
            c.execute_query('{"table": "secrets"}')
        with pytest.raises(ValueError):
            c.execute_query('{"limit": 5}')

    def test_dict_spec_accepted_and_query_alias(self, fake_api):
        c = make_client()
        assert len(c.query({"table": "namespaces"})) >= 2


# ── test_connection ───────────────────────────────────────────────────────────


class TestConnection:
    def test_success_reports_counts_and_metrics(self, fake_api):
        res = make_client().test_connection()
        assert res["success"] is True
        assert re.search(r"\d+ nodes", res["message"]) and re.search(r"\d+ namespaces", res["message"])
        assert "metrics-server" in res["message"]

    def test_not_cluster_wide_is_a_failure_pointing_at_rbac(self, fake_api):
        fake_api.overrides["/api/v1/nodes"] = api_error(403, 'nodes is forbidden: User "system:serviceaccount:bagofwords:reader" cannot list resource "nodes"')
        res = make_client().test_connection()
        assert res["success"] is False and "rbac.yaml" in res["message"]

    def test_expired_token_401(self, fake_api):
        fake_api.overrides["/version"] = api_error(401, "Unauthorized")
        res = make_client().test_connection()
        assert res["success"] is False and "expired" in res["message"].lower()

    def test_transport_failure_names_the_server(self, fake_api):
        import urllib3.exceptions
        fake_api.overrides["/version"] = urllib3.exceptions.MaxRetryError(None, "https://10.0.0.1:6443", "connection refused")
        res = make_client().test_connection()
        assert res["success"] is False and "10.0.0.1:6443" in res["message"]

    def test_bearer_header_and_ca_configured(self, fake_api):
        c = make_client()
        with c.connect() as api:
            cfg = api.configuration
        assert cfg.api_key["authorization"] == f"Bearer {JWT}"
        assert cfg.host == "https://10.0.0.1:6443"
        assert cfg.ssl_ca_cert and pathlib.Path(cfg.ssl_ca_cert).read_text() == CA_PEM


# ── registry ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_entry_is_single_system_scoped_variant(self):
        from app.schemas.data_source_registry import get_entry, resolve_client_class

        entry = get_entry("kubernetes")
        assert resolve_client_class("kubernetes") is KubernetesClient
        assert list(entry.credentials_auth.by_auth) == ["access_file"]
        assert entry.credentials_auth.by_auth["access_file"].scopes == ["system"]
        assert entry.category == "infra" and entry.requires_license == "enterprise" and entry.is_connection

    def test_setup_guide_has_three_steps_and_pins_the_script(self):
        from app.schemas.data_source_registry import (
            KUBERNETES_PRINT_ACCESS_FILE_SCRIPT, KUBERNETES_RBAC_MANIFEST, get_entry, list_available_data_sources,
        )

        steps = get_entry("kubernetes").setup_guide
        assert [bool(s.code) for s in steps] == [True, True, False]
        tools = pathlib.Path(__file__).parents[3] / "tools" / "kubernetes"
        # Step 1 shows the exact manifest the admin applies — inline, not a URL.
        assert KUBERNETES_RBAC_MANIFEST == (tools / "rbac.yaml").read_text()
        assert steps[0].code.startswith("kubectl apply -f - <<'EOF'\n") and steps[0].code.endswith("\nEOF")
        assert "kind: ClusterRole" in steps[0].code and "http" not in steps[0].code.split("\n")[0]
        assert KUBERNETES_PRINT_ACCESS_FILE_SCRIPT == (tools / "print_access_file.sh").read_text()
        served = next(d for d in list_available_data_sources() if d["type"] == "kubernetes")
        assert len(served["setup_guide"]) == 3 and served["setup_guide"][1]["code"] == KUBERNETES_PRINT_ACCESS_FILE_SCRIPT

    def test_only_visible_config_field_is_namespaces(self):
        from app.schemas.data_sources.configs import KubernetesAccessFileCredentials, KubernetesConfig

        visible = [n for n, f in KubernetesConfig.model_fields.items()
                   if not (f.json_schema_extra or {}).get("ui:hidden")]
        assert visible == ["namespaces"]
        assert list(KubernetesAccessFileCredentials.model_fields) == ["access_file"]

    def test_no_user_scoped_variant_so_no_require_user_auth(self):
        from app.schemas.data_source_registry import supports_user_auth

        assert supports_user_auth("kubernetes") is False
        assert supports_user_auth("postgresql") is True      # the toggle still shows where it applies
        assert supports_user_auth("no-such-type") is True     # unknown → never hide by accident

    def test_enterprise_gated(self):
        from app.ee.license import ENTERPRISE_DATASOURCES

        assert "kubernetes" in ENTERPRISE_DATASOURCES
