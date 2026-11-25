"""Teamcenter connector contract against the in-process mock (tools/teamcenter/mock_teamcenter_server.py).

The mock is the remote boundary: a real HTTP server speaking Teamcenter's SOA JSON shapes (web tier,
Security Services login, file server), with per-user access rules, cookie sessions with an XSRF token,
saved queries, BOM windows under revision rules, and a "legacy" profile that only offers the older
service versions. Everything on our side (client, schemas, registry) runs for real.
"""
import importlib.util
import json
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pydantic
import pytest

from app.data_sources.clients import teamcenter_client as tc
from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
from app.data_sources.clients.base import Capability
from app.schemas.data_source_registry import REGISTRY, resolve_client_class
from app.schemas.data_sources.configs import TeamcenterConfig

_MOCK_PATH = Path(__file__).resolve().parents[3] / "tools" / "teamcenter" / "mock_teamcenter_server.py"
PART_TYPES = "A2_PartRevision, ItemRevision, ChangeNoticeRevision, ChangeRequestRevision, ProblemReportRevision"


def _load_mock():
    if "teamcenter_mock" in sys.modules:
        return sys.modules["teamcenter_mock"]
    spec = importlib.util.spec_from_file_location("teamcenter_mock", _MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["teamcenter_mock"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def tc_server():
    mock = _load_mock()
    server = mock.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    root = f"http://{host}:{port}"
    yield {"mock": mock, "base_url": f"{root}/tc", "root": root, "login_url": f"{root}/tcssol/weblogin/login_redirect"}
    server.shutdown()
    server.server_close()


@pytest.fixture
def tcm(tc_server):
    tc_server["mock"].reset()
    tc.clear_cache()
    yield tc_server
    tc.clear_cache()
    tc_server["mock"].reset()


def client(tcm, user="jdoe", **kw):
    kw.setdefault("base_url", tcm["base_url"])
    kw.setdefault("allow_http", True)
    kw.setdefault("object_types", PART_TYPES)
    if not any(k in kw for k in ("access_token", "use_kerberos", "username")):
        kw["username"] = user
        kw["password"] = tcm["mock"].USERS[user]["password"]
    return tc.TeamcenterClient(**kw)


def q(c, **spec):
    return c.execute_query(json.dumps(spec))


def ops(tcm, name):
    return [entry for entry in tcm["mock"].REQUEST_LOG if entry[1].endswith("/" + name)]


# ------------------------------------------------------------------ registry and config
def test_registry_entry_resolves_and_offers_every_sign_in():
    entry = REGISTRY["teamcenter"]
    assert resolve_client_class("teamcenter") is tc.TeamcenterClient
    assert entry.config_schema is TeamcenterConfig
    variants = entry.credentials_auth.by_auth
    assert {"userpass", "kerberos", "kerberos_delegated", "oauth"} <= set(variants)
    assert "user" in variants["oauth"].scopes and "user" in variants["userpass"].scopes
    assert variants["kerberos"].scopes == ["system"]


@pytest.mark.parametrize("bad", [
    {"base_url": "plm.example.com"},
    {"base_url": "https://plm.example.com/tc", "sso_login_url": "https://plm/tcssol/weblogin/login_redirect"},
    {"base_url": "https://plm.example.com/tc", "object_types": "Item Revision; DROP"},
    {"base_url": "https://plm.example.com/tc", "sso_app_id": "BOW", "sso_login_url": "ftp://x"},
])
def test_config_rejects_what_cannot_work(bad):
    with pytest.raises(pydantic.ValidationError):
        TeamcenterConfig(**bad)


def test_https_is_required_unless_a_lab_allows_http(tcm):
    with pytest.raises(tc.TeamcenterError):
        client(tcm, allow_http=False)
    with pytest.raises(tc.TeamcenterError):
        client(tcm, base_url="http://u:p@host/tc")


# ------------------------------------------------------------------ sign-in and diagnostics
def test_test_connection_names_the_identity_and_reports_each_capability(tcm):
    out = client(tcm).test_connection()
    assert out["success"] is True, out
    assert out["details"]["identity"] == "jdoe"
    assert set(out["details"]["checks"]) == {"object_types", "saved_queries", "revision_rule", "documents"}
    assert all(check["ok"] for check in out["details"]["checks"].values()), out


def test_a_wrong_password_is_a_refused_login_without_the_secret(tcm):
    out = client(tcm, username="jdoe", password="not-the-pw-7f3").test_connection()
    assert out["success"] is False and out["login_refused"] == "rejected"
    assert "not-the-pw-7f3" not in json.dumps(out) and tcm["root"] not in json.dumps(out)


def test_missing_types_and_queries_are_reported_not_hidden(tcm):
    out = client(tcm, object_types="NoSuchRevision", saved_queries="Nope...").test_connection()
    assert out["success"] is False and out.get("reachable") is True
    checks = out["details"]["checks"]
    assert not checks["object_types"]["ok"] and not checks["saved_queries"]["ok"]


def test_the_web_tier_url_may_be_pasted_with_the_services_path(tcm):
    c = client(tcm, base_url=tcm["base_url"] + "/JsonRestServices/")
    assert c.test_connection()["success"] is True


def test_a_non_teamcenter_url_says_so(tcm):
    out = client(tcm, base_url=tcm["root"] + "/elsewhere").test_connection()
    assert out["success"] is False and "JsonRestServices" in out["message"]


def test_older_servers_are_served_by_the_older_service_versions(tcm):
    tcm["mock"].STATE["profile"] = "legacy"
    c = client(tcm)
    assert c.test_connection()["success"] is True
    df = q(c, table="a2_part_revision", filter={"item_id": "PRT-1002"}, columns=["item_id", "object_name"])
    assert df["item_id"].tolist() == ["PRT-1002"]
    assert ops(tcm, "executeSavedQuery") and not ops(tcm, "executeSavedQueries")
    # Without type metadata the tables still have the standard columns.
    cols = {c.name for c in client(tcm).get_schema("a2_part_revision").columns}
    assert {"item_id", "item_revision_id", "object_name", "release_status_list"} <= cols


def test_sessions_are_reused_and_restored_once_when_teamcenter_ends_them(tcm):
    q(client(tcm), table="revision_rules")
    q(client(tcm), table="a2_part_revision", filter={"item_id": "PRT-1000"})
    assert len(ops(tcm, "login")) == 1, "one identity, one Teamcenter session"
    tcm["mock"].STATE["expire_sessions"] = True
    df = q(client(tcm), table="a2_part_revision", filter={"item_id": "PRT-1001"}, columns=["item_id"])
    assert set(df["item_id"]) == {"PRT-1001"} and len(ops(tcm, "login")) == 2
    q(client(tcm, user="asmith"), table="revision_rules")
    assert len(ops(tcm, "login")) == 3, "another identity gets its own session"


def test_a_password_on_an_sso_only_site_is_refused(tcm):
    tcm["mock"].STATE["sso_only"] = True
    out = client(tcm).test_connection()
    assert out["success"] is False and out["login_refused"] == "rejected"


# ------------------------------------------------------------------ catalog
def test_each_existing_type_is_a_table_with_its_custom_properties(tcm):
    tables = {t.name: t for t in client(tcm, object_types=PART_TYPES + ", NoSuchRevision").get_schemas()}
    assert {"a2_part_revision", "item_revision", "change_notice_revision", "bom", "where_used", "change_items",
            "revision_rules"} <= set(tables)
    assert "no_such_revision" not in tables
    part_cols = {c.name for c in tables["a2_part_revision"].columns}
    assert {"a2_material", "a2_mass_kg", "a2_make_buy", "item_id", "release_status_list"} <= part_cols
    assert not any(c.startswith(("fnd0", "awp0")) for c in part_cols), "Siemens internals stay out"
    assert {"CMMaturity", "CMClosure"} <= {c.name for c in tables["change_notice_revision"].columns}
    assert [p.name for p in tables["a2_part_revision"].pks] == ["uid"]


def test_change_links_need_a_change_type(tcm):
    names = {t.name for t in client(tcm, object_types="A2_PartRevision").get_schemas()}
    assert "change_items" not in names and "bom" in names


# ------------------------------------------------------------------ object tables
def test_rows_are_the_objects_of_exactly_that_type(tcm):
    c = client(tcm, user="infodba")
    for table, type_name in (("a2_part_revision", "A2_PartRevision"), ("item_revision", "ItemRevision")):
        df = q(c, table=table, columns=["item_id", "object_type"])
        assert len(df) > 0 and set(df["object_type"]) == {type_name}


@pytest.mark.parametrize("flt,expected", [
    ({"item_id": "PRT-10*"}, {"PRT-1000", "PRT-1001", "PRT-1002", "PRT-1004", "PRT-1005"}),
    ({"item_id": "prt-2000"}, {"PRT-2000"}),
    ({"a2_make_buy": "Buy"}, {"PRT-1002"}),
    ({"a2_material": "*stainless*", "item_revision_id": "A"}, {"PRT-1002", "PRT-1005"}),
    ({"a2_mass_kg": {"gte": 5}}, {"PRT-1000", "PRT-1004", "PRT-2000"}),
    ({"release_status_list": ""}, {"PRT-1000", "PRT-1001", "PRT-1004"}),
    ({"last_mod_date": {"gte": "2026-09-01"}}, {"PRT-1000", "PRT-1001", "PRT-1004"}),
])
def test_filters_select_the_matching_parts(tcm, flt, expected):
    df = q(client(tcm), table="a2_part_revision", filter=flt, columns=["item_id"])
    assert set(df["item_id"]) == expected


@pytest.mark.parametrize("flt,expected", [
    ({"creation_date": "2026-06-02"}, {("PRT-1000", "B")}),
    ({"creation_date": "2026-06"}, {("PRT-1000", "B"), ("PRT-1001", "B")}),
    ({"creation_date": {"gte": "2026-06-01"}}, {("PRT-1000", "B"), ("PRT-1001", "B")}),
])
def test_a_date_filter_is_never_turned_into_a_range_entry(tcm, flt, expected):
    """Real object queries carry "Created After/Before" entries on creation_date: an equality filter must
    not be sent there (it would silently become "created after")."""
    df = q(client(tcm), table="a2_part_revision", filter=flt, columns=["item_id", "item_revision_id"])
    assert set(zip(df["item_id"], df["item_revision_id"])) == expected


def test_typed_columns_and_the_row_limit(tcm):
    df = q(client(tcm), table="a2_part_revision", columns=["item_id", "a2_mass_kg", "last_mod_date"], limit=3)
    assert len(df) == 3
    assert str(df["a2_mass_kg"].dtype).startswith("float") and str(df["last_mod_date"].dtype).startswith("datetime64")


def test_teamcenter_access_rules_decide_what_a_user_sees(tcm):
    hidden = "PRT-1003"
    assert hidden not in set(q(client(tcm, user="jdoe"), table="a2_part_revision")["item_id"])
    assert hidden in set(q(client(tcm, user="infodba"), table="a2_part_revision")["item_id"])
    assert hidden not in set(q(client(tcm, user="jdoe"), table="bom", root="PRT-1004/A")["item_id"])
    assert hidden in set(q(client(tcm, user="infodba"), table="bom", root="PRT-1004/A")["item_id"])


@pytest.mark.parametrize("spec", [
    "not json", {"table": "nope"}, {"table": "a2_part_revision", "columns": ["nope"]},
    {"table": "a2_part_revision", "limit": 0}, {"table": "a2_part_revision", "filter": ["x"]},
    {"table": "a2_part_revision", "filter": {"a2_mass_kg": {"around": 3}}},
    {"table": "bom"}, {"table": "where_used"},
])
def test_bad_specs_get_a_fixed_message(tcm, spec):
    with pytest.raises(tc.TeamcenterError) as exc:
        client(tcm).execute_query(spec if isinstance(spec, str) else json.dumps(spec))
    assert tcm["root"] not in str(exc.value)


# ------------------------------------------------------------------ relations
def test_the_bom_follows_the_revision_rule_and_always_closes_its_window(tcm):
    c = client(tcm, user="infodba")
    working = q(c, table="bom", root="PRT-1000/B", depth=3)
    by_item = {(r.item_id, r.level): r for r in working.itertuples()}
    assert by_item[("PRT-1001", 1)].revision == "B"            # Latest Working
    assert by_item[("PRT-1005", 2)].quantity == 7.0             # quantity per parent (rev B impeller)
    assert by_item[("PRT-1002", 1)].find_number == "20"
    assert set(working["parent_item_id"]) == {"PRT-1000", "PRT-1001", "PRT-1004"}
    released = q(c, table="bom", root="PRT-1000/B", revision_rule="Latest Released", depth=1)
    assert set(released.loc[released.item_id == "PRT-1001", "revision"]) == {"A"}
    assert "PRT-1004" not in set(released["item_id"]), "nothing released to configure"
    assert set(q(c, table="bom", root="PRT-1000/B", depth=1)["level"]) == {1}
    assert tcm["mock"].OPEN_WINDOWS["count"] == 0
    with pytest.raises(tc.TeamcenterError):
        q(c, table="bom", root="PRT-1000/B", revision_rule="No Such Rule")
    assert tcm["mock"].OPEN_WINDOWS["count"] == 0


def test_the_bom_root_defaults_to_the_newest_revision(tcm):
    df = q(client(tcm), table="bom", root="PRT-1000", depth=1)
    assert "PRT-1004" in set(df["item_id"])          # only rev B has the housing


def test_where_used_walks_up_one_level_or_all(tcm):
    c = client(tcm, user="infodba")
    direct = q(c, table="where_used", item="PRT-1003/A")
    assert set(direct["parent_item_id"]) == {"PRT-1004"} and set(direct["level"]) == {1}
    every = q(c, table="where_used", item="PRT-1003", levels="all")
    assert {"PRT-1004", "PRT-1000"} <= set(every["parent_item_id"]) and max(every["level"]) == 2


def test_change_links_by_change_and_by_change_filter(tcm):
    c = client(tcm)
    one = q(c, table="change_items", change="ECN-0001")
    assert set(one["change_id"]) == {"ECN-0001"}
    assert {("impacted", "PRT-1001", "A"), ("solution", "PRT-1001", "B")} <= set(
        zip(one["relation"], one["item_id"], one["item_revision"]))
    # The change's own state and the linked item's are separate columns.
    assert set(one["change_closure"]) == {"Open"} and set(one["change_maturity"]) == {"Executing"}
    assert set(one.loc[one.item_revision == "B", "item_release_status"].fillna("")) == {""}
    open_changes = q(c, table="change_items", filter={"CMClosure": "Open"})
    assert set(open_changes["change_id"]) == {"ECN-0001", "PR-0001"} and set(open_changes["change_closure"]) == {"Open"}
    impeller = q(c, table="change_items", filter={"change_closure": "Open", "item_id": "PRT-1001"})
    assert set(impeller["change_id"]) == {"ECN-0001", "PR-0001"} and set(impeller["item_id"]) == {"PRT-1001"}
    solutions = q(c, table="change_items", filter={"relation": "solution"})
    assert set(solutions["relation"]) == {"solution"} and "ECN-0002" in set(solutions["change_id"])


# ------------------------------------------------------------------ documents
def test_documents_are_listed_searched_and_read_but_cad_is_not(tcm):
    c = client(tcm)
    attached = c.list_files("PRT-1000/B")
    assert [f["name"] for f in attached] == ["PRT-1000 Pump Assembly Specification.pdf"]
    home = c.list_files()
    assert {"folder", "item", "document"} <= {f["kind"] for f in home}
    pump = next(f for f in home if f["kind"] == "folder")
    assert any(f["name"].endswith(".docx") for f in c.list_files(pump["id"]))
    found = c.search_files("impeller report")
    assert [f["id"] for f in found] == ["dataset:DS_report_docx01"]
    text = c.read_file(attached[0]["id"])
    assert tcm["mock"].SPEC_CODE in str(text)
    assert "210 MPa" in str(c.read_file("dataset:DS_report_docx01"))
    with pytest.raises(GlobScopeError):
        c.read_file("dataset:DS_model_prt0001")
    with pytest.raises(tc.TeamcenterError):
        c.read_file("../../etc/passwd")


@pytest.mark.parametrize("question,expected", [
    ("PRT-1000 specification maximum operating pressure design code", "dataset:DS_spec_pdf00001"),
    ("pump assembly spec", "dataset:DS_spec_pdf00001"),
    ("what does the impeller test report say about fatigue?", "dataset:DS_report_docx01"),
    ("PRT-1001", "dataset:DS_report_docx01"),
])
def test_a_question_finds_its_document_first(tcm, question, expected):
    found = client(tcm).search_files(question)
    assert found and found[0]["id"] == expected, found
    assert all(f["id"] != "dataset:DS_model_prt0001" for f in found), "CAD is never offered"


def test_a_search_for_nothing_known_finds_nothing(tcm):
    assert client(tcm).search_files("quarterly marketing budget") == []
    assert client(tcm).search_files("the and of") == []


def test_document_size_limit_and_documents_switched_off(tcm):
    with pytest.raises(FileTooLargeError):
        client(tcm).read_raw_bytes("dataset:DS_spec_pdf00001", max_bytes=10)
    off = client(tcm, include_documents=False)
    assert off.capabilities == {Capability.QUERY}
    with pytest.raises(tc.TeamcenterError):
        off.search_files("pump")


def test_a_hidden_items_documents_stay_hidden(tcm):
    with pytest.raises(tc.TeamcenterError):
        client(tcm).list_files("PRT-1003/A")


# ------------------------------------------------------------------ Teamcenter Security Services
def _sso_cfg(tcm, **extra):
    return {"sso_login_url": tcm["login_url"], "sso_app_id": "BOW", "allow_http": True, **extra}


def test_the_login_url_carries_the_application_id(tcm):
    url = tc.tcss_login_url(_sso_cfg(tcm))
    assert dict(parse_qsl(urlsplit(url).query)) == {"appID": "BOW"}
    custom = tc.tcss_login_url(_sso_cfg(tcm, sso_app_id_param="TCSS_AppID"))
    assert dict(parse_qsl(urlsplit(custom).query)) == {"TCSS_AppID": "BOW"}
    with pytest.raises(tc.TeamcenterError):
        tc.tcss_login_url({"sso_login_url": tcm["login_url"]})


@pytest.mark.parametrize("params,names,expected", [
    ({"TCSS_TOKEN": "t1", "TCSS_USERID": "jdoe"}, (None, None), ("t1", "jdoe")),
    ([("ssoAppToken", "t2"), ("tcUser", "x")], (None, None), ("t2", "x")),
    ({"tok": "t3", "u": "y", "token_other": "z"}, ("tok", "u"), ("t3", "y")),
    ({"state": "s"}, (None, None), (None, None)),
])
def test_the_token_and_user_are_read_from_whatever_tcss_sends(params, names, expected):
    assert tc.extract_sso_result(params, *names) == expected


def _browser_token(tcm, user):
    """What a browser gets back from TcSS after the user signs in with the password form."""
    import requests
    tcm["mock"].STATE["app_roots"]["BOW"] = "https://bow.example.com/api/connections/teamcenter/sso/callback"
    form = requests.post(tcm["root"] + "/tcssol/weblogin/login", allow_redirects=False,
                         data={"appID": "BOW", "username": user, "password": tcm["mock"].USERS[user]["password"]})
    assert form.status_code == 302
    return tc.extract_sso_result(parse_qsl(urlsplit(form.headers["Location"]).query))


def test_a_tcss_token_signs_the_member_in_as_themselves(tcm):
    token, user = _browser_token(tcm, "jdoe")
    c = client(tcm, access_token=token, tcss_user=user)
    out = c.test_connection()
    assert out["success"] is True and out["details"]["identity"] == "jdoe" and c.auth_method == "sso_token"
    assert "PRT-1003" not in set(q(c, table="a2_part_revision")["item_id"])
    assert ops(tcm, "loginSSO") and not ops(tcm, "login")


def test_an_expired_tcss_token_asks_for_a_new_sign_in(tcm):
    out = client(tcm, access_token="expired-token-1", tcss_user="jdoe").test_connection()
    assert out["success"] is False and out["login_refused"] == "rejected"
    assert "SSO" in out["message"] and "expired-token-1" not in json.dumps(out)


class _FakeGss:
    """Stands in for python-gssapi at the Kerberos boundary: yields an opaque initial token."""

    class NameType:
        hostbased_service = "hostbased"

    class OID:
        @staticmethod
        def from_int_seq(seq):
            return seq

    def Credentials(self, usage):
        return object()

    def Name(self, name, kind):
        return name

    def SecurityContext(self, name, creds, usage, mech):
        class Ctx:
            def step(self):
                return b"\x60\x82kerberos-initial-token"
        return Ctx()


class _FakeTickets:
    def __init__(self):
        self.asked = []

    def service_ccache(self, principal=None):
        self.asked.append(("service", principal))

    def delegated_ccache(self, user):
        self.asked.append(("delegated", user))
        return "/tmp/ccache"

    def activate(self, ccache):
        import contextlib
        return contextlib.nullcontext()


@pytest.fixture
def kerberos(monkeypatch):
    import app.data_sources.kerberos as krb
    tickets = _FakeTickets()
    monkeypatch.setattr(krb, "_import_gssapi", lambda: _FakeGss())
    monkeypatch.setattr(krb, "get_ticket_manager", lambda: tickets)
    return tickets


def test_kerberos_answers_the_login_service_and_redeems_its_token(tcm, kerberos):
    tcm["mock"].STATE["app_roots"]["BOW"] = "https://bow.example.com/cb"
    tcm["mock"].STATE["kerberos_user"] = "asmith"
    c = client(tcm, use_kerberos=True, kerberos_principal="svc_bow@EXAMPLE.COM", **_sso_cfg(tcm))
    out = c.test_connection()
    assert out["success"] is True and out["details"]["identity"] == "asmith", out
    assert kerberos.asked == [("service", "svc_bow@EXAMPLE.COM")]


def test_kerberos_delegation_signs_in_the_member(tcm, kerberos):
    tcm["mock"].STATE["app_roots"]["BOW"] = "https://bow.example.com/cb"
    tcm["mock"].STATE["kerberos_user"] = "jdoe"
    with pytest.raises(tc.TeamcenterError):
        client(tcm, use_kerberos=True, kerberos_impersonate="jdoe@EXAMPLE.COM", **_sso_cfg(tcm))
    c = client(tcm, use_kerberos=True, kerberos_impersonate="jdoe@EXAMPLE.COM", kerberos_expected_sid="0105",
               **_sso_cfg(tcm))
    assert c.test_connection()["details"]["identity"] == "jdoe"
    assert kerberos.asked == [("delegated", "jdoe@EXAMPLE.COM")]


def test_a_login_service_without_windows_sign_in_says_so(tcm, kerberos):
    tcm["mock"].STATE["app_roots"]["BOW"] = "https://bow.example.com/cb"   # no kerberos_user: password form
    out = client(tcm, use_kerberos=True, **_sso_cfg(tcm)).test_connection()
    assert out["success"] is False and "Windows integrated" in out["message"]
