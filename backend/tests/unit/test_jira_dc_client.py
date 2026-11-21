"""Jira Data Center connector contract against the in-process mock REST API.

The mock (tools/jira/mock_jira_server.py) is the remote boundary: a real HTTP
server speaking the Jira Data Center REST (v2) and agile (1.0) shapes under a
`/jira` context path, with a JQL engine, per-user project access, issue
security, silent page clamping, embedded collections cut off at 20, and the
on-prem failure modes. Everything on our side (client, schemas, registry) runs
for real. Call counts are asserted because "few calls per query" is a promise
of the design (docs/design/jira-data-center-connector.md), not an accident.
"""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pydantic
import pytest

from app.data_sources.clients import _atlassian_dc_http as dc
from app.data_sources.clients import jira_dc_client as jc
from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
from app.data_sources.clients.base import Capability, SearchStopped
from app.schemas.data_source_registry import REGISTRY, resolve_client_class

_MOCK_PATH = Path(__file__).resolve().parents[3] / "tools" / "jira" / "mock_jira_server.py"


def _load_mock():
    if "jira_dc_mock" in sys.modules:
        return sys.modules["jira_dc_mock"]
    spec = importlib.util.spec_from_file_location("jira_dc_mock", _MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["jira_dc_mock"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def jira_server():
    mock = _load_mock()
    server = mock.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    yield {"mock": mock, "base_url": f"http://{host}:{port}/jira", "root": f"http://{host}:{port}"}
    server.shutdown()
    server.server_close()


@pytest.fixture
def jira(jira_server, monkeypatch):
    jira_server["mock"].reset()
    jc.clear_cache()
    monkeypatch.setattr(dc, "_sleep", lambda s: None)
    yield jira_server
    jira_server["mock"].reset()
    jc.clear_cache()


def _token(jira, user):
    return next(t for t, u in jira["mock"].TOKENS.items() if u == user)


def client(jira, user="bob", **kw):
    kw.setdefault("base_url", jira["base_url"])
    kw.setdefault("allow_http", True)
    if "username" not in kw and "personal_access_token" not in kw:
        kw["personal_access_token"] = _token(jira, user)
    return jc.JiraDcClient(**kw)


def basic(jira, user="bob", password=None, **kw):
    return client(jira, username=user, password=password or jira["mock"].USERS[user]["password"], **kw)


def q(c, **spec):
    return c.execute_query(json.dumps(spec))


def searches(jira):
    return [row for row in jira["mock"].REQUEST_LOG if row[1].endswith("/rest/api/2/search")]


# ----------------------------------------------------------------------------- registry and schemas

def test_registry_entry_is_a_hybrid_per_user_source():
    entry = REGISTRY["jira_dc"]
    assert resolve_client_class("jira_dc") is jc.JiraDcClient
    assert set(entry.credentials_auth.by_auth) == {"token", "userpass"}
    for variant in entry.credentials_auth.by_auth.values():
        assert set(variant.scopes) == {"system", "user"}
    assert {Capability.QUERY, Capability.READ_FILE, Capability.SEARCH_FILES} <= jc.JiraDcClient.capabilities


def test_config_schema_validates_projects_and_tables():
    cfg = REGISTRY["jira_dc"].config_schema
    assert cfg(base_url="https://j.example.com", projects=" ops,hr ").projects == "OPS, HR"
    assert cfg(base_url="https://j.example.com", tables="issues, sprints").tables == "issues, sprints"
    with pytest.raises(pydantic.ValidationError):
        cfg(base_url="https://j.example.com", projects="OPS-1")
    with pytest.raises(pydantic.ValidationError):
        cfg(base_url="https://j.example.com", tables="issues, secrets")


def test_token_schema_never_echoes_a_rejected_secret():
    schema = REGISTRY["jira_dc"].credentials_auth.by_auth["token"].schema
    with pytest.raises(pydantic.ValidationError) as err:
        schema(personal_access_token="has space-secret-71x")
    assert "has space-secret-71x" not in str(err.value)


def test_a_pasted_api_suffix_is_dropped_and_the_context_path_kept(jira):
    c = client(jira, base_url=jira["base_url"] + "/rest/api/2/")
    assert c.context_path == "/jira"
    assert c.test_connection()["success"] is True


# ----------------------------------------------------------------------------- connection / auth

@pytest.mark.parametrize("make", [lambda j: client(j, "bob"), lambda j: basic(j, "bob")], ids=["token", "userpass"])
def test_connection_names_the_jira_identity(jira, make):
    result = make(jira).test_connection()
    assert result["success"] is True, result
    assert result["details"]["identity"] == "bob"


def test_a_wrong_password_is_a_refused_login_and_never_leaks(jira):
    result = basic(jira, "bob", password="wrong-jira-pass-3e1").test_connection()
    assert result["success"] is False and result["login_refused"] == "rejected"
    assert "wrong-jira-pass-3e1" not in str(result)


def test_captcha_and_disabled_tokens_are_named(jira):
    jira["mock"].STATE["captcha_users"] = {"bob"}
    assert basic(jira, "bob").test_connection()["login_refused"] == "captcha"
    jira["mock"].STATE["pats_disabled"] = True
    result = client(jira, "bob").test_connection()
    assert result["login_refused"] == "rejected" and "personal access token" in result["message"]


@pytest.mark.parametrize("switch", ["sso_redirect", "html_page"])
def test_sso_and_proxy_pages_are_named_not_followed(jira, switch):
    jira["mock"].STATE[switch] = True
    result = client(jira).test_connection()
    assert result["success"] is False and "login_refused" not in result
    assert len(jira["mock"].REQUEST_LOG) == 1


def test_a_missing_context_path_is_named_as_not_the_rest_api(jira):
    result = client(jira, base_url=jira["root"]).test_connection()
    assert result["success"] is False and "context path" in result["message"]


def test_projects_the_identity_cannot_see_fail_the_test_but_stay_savable(jira):
    result = client(jira, "bob", projects="OPS, SEC").test_connection()
    assert result["success"] is False and result["reachable"] is True and "SEC" in result["message"]
    assert client(jira, "alice", projects="OPS, SEC").test_connection()["success"] is True


# ----------------------------------------------------------------------------- JQL scope

def test_the_sent_jql_wraps_the_model_filter_inside_the_scope():
    c = jc.JiraDcClient(base_url="https://j.example.com", personal_access_token="t0k", projects="OPS,HR")
    assert c.scoped_jql("status = Done ORDER BY created DESC") == \
        'project in ("OPS", "HR") AND (status = Done) ORDER BY created DESC'
    assert c.scoped_jql("") == 'project in ("OPS", "HR") ORDER BY key ASC'
    # ORDER BY inside a quoted value is text, not the order clause.
    assert c.scoped_jql('summary ~ "order by date"').startswith('project in ("OPS", "HR") AND (summary ~ "order by date")')


@pytest.mark.parametrize("jql", ['x) OR (project = SEC', 'status = "Done', "(status = Done", "a = b)) OR ((c = d",
                                 'summary ~ "x\\'])
def test_unbalanced_jql_is_refused_before_any_request(jira, jql):
    c = client(jira, projects="OPS")
    with pytest.raises(jc.JiraError):
        q(c, table="issues", jql=jql)
    assert jira["mock"].REQUEST_LOG == []


@pytest.mark.parametrize("jql", ["project = SEC", "(status = Done) OR (project = SEC)", "project in (SEC, HR)",
                                 "key = SEC-1 OR project = HR"])
def test_balanced_jql_cannot_reach_outside_the_scope(jira, jql):
    df = q(client(jira, "alice", projects="OPS"), table="issues", jql=jql, fields=["key", "project"])
    assert set(df["project"]) <= {"OPS"}


def test_rows_from_outside_the_scope_are_dropped_even_if_jira_returns_them(jira, monkeypatch):
    c = client(jira, "alice", projects="OPS")
    monkeypatch.setattr(c, "scoped_jql", lambda jql: "ORDER BY key ASC")    # a JQL layer that failed open
    df = q(c, table="issues", fields=["key", "project"])
    assert set(df["project"]) == {"OPS"} and len(df) == 12


# ----------------------------------------------------------------------------- issues

def test_issues_run_as_the_identity_and_hide_secured_issues(jira):
    bob = set(q(client(jira, "bob"), table="issues", jql="project = OPS")["key"])
    alice = set(q(client(jira, "alice"), table="issues", jql="project = OPS")["key"])
    assert "OPS-7" not in bob and "OPS-7" in alice


def test_issue_columns_flatten_fields_custom_fields_and_sprints(jira):
    df = q(client(jira), table="issues", jql="key = OPS-4")
    row = df.iloc[0]
    assert row["status"] == "In Progress" and row["status_category"] == "In Progress"
    assert row["assignee"] == "Bob Builder" and row["assignee_username"] == "bob"
    assert row["story_points"] == 5.0 and row["sprint"] == "OPS Sprint 43" and row["sprint_ids"] == "43"
    assert row["epic_link"] == "OPS-10" and set(row["components"].split(", ")) == {"API", "Database"}
    assert row["fix_versions"] == "2.0"
    assert str(df["created"].dtype).startswith("datetime64") and row["web_url"].endswith("/jira/browse/OPS-4")


def test_an_issue_query_is_one_search_call_requesting_only_the_needed_fields(jira):
    c = client(jira)
    df = q(c, table="issues", jql="project = OPS", fields=["key", "status", "story_points"])
    assert len(df) == 11
    calls = searches(jira)
    assert len(calls) == 1
    sent = calls[0][2]
    assert "fields=project%2Cstatus%2Ccustomfield_10016" in sent and "expand" not in sent


def test_paging_survives_a_silent_page_clamp(jira):
    jira["mock"].STATE["max_results_cap"] = 4
    df = q(client(jira, "alice"), table="issues", jql="project = OPS", fields=["key"])
    assert len(df) == 12 and df["key"].is_unique
    assert len(searches(jira)) == 3


def test_limit_and_unknown_columns(jira):
    c = client(jira)
    assert len(q(c, table="issues", jql="project = OPS", limit=3)) == 3
    with pytest.raises(jc.JiraError) as err:
        q(c, table="issues", fields=["key", "nope"])
    assert "nope" in str(err.value)
    with pytest.raises(jc.JiraError):
        q(c, table="secrets")


def test_jql_jira_rejects_is_a_fixed_message(jira):
    with pytest.raises(jc.JiraHTTPError) as err:
        q(client(jira), table="issues", jql="nosuchfield = 1")
    assert err.value.kind == "bad_request" and "fields" in str(err.value)


# ----------------------------------------------------------------------------- child tables: few calls

def test_changelog_comes_from_the_same_search(jira):
    c = client(jira)
    df = q(c, table="issue_changelog", jql="project = OPS AND resolved >= -40d")
    assert c.calls == 1
    status = df[df["field"] == "status"]
    assert {"In Progress", "Done"} <= set(status["to_value"])


def test_a_cut_off_changelog_costs_one_call_per_affected_issue_only(jira):
    jira["mock"].STATE["changelog_cap"] = 2
    c = client(jira)
    df = q(c, table="issue_changelog", jql="key in (OPS-1, OPS-5, OPS-6)")
    # OPS-1 has 3 histories (cut to 2); OPS-5 has none and OPS-6 has 2: one extra call.
    assert c.calls == 2
    assert len(df[df["issue_key"] == "OPS-1"]["history_id"].unique()) == 3


def test_comments_are_fetched_only_for_issues_jira_cut_off(jira):
    c = client(jira)
    df = q(c, table="issue_comments", jql="project = OPS")
    assert df.groupby("issue_key").size()["OPS-1"] == 25
    assert c.calls == 2      # one search + one comment page for OPS-1 (25 > 20 embedded)


def test_worklogs_are_fetched_only_for_issues_jira_cut_off(jira):
    c = client(jira)
    df = q(c, table="worklogs", jql="project = OPS")
    assert df.groupby("issue_key").size()["OPS-2"] == 23
    assert c.calls == 2
    assert df["hours"].sum() == pytest.approx(df["time_spent_seconds"].sum() / 3600, rel=1e-3)


def test_links_and_attachments_cost_no_extra_calls(jira):
    c = client(jira)
    links = q(c, table="issue_links", jql="project = OPS")
    files = q(c, table="attachments", jql="project = OPS")
    assert c.calls == 2
    assert {"blocks", "relates to", "is duplicated by"} == set(links["relation"])
    assert files.iloc[0]["filename"] == "failover-timeline.png"


def test_too_many_follow_ups_stop_with_a_fixed_error_not_partial_data(jira, monkeypatch):
    monkeypatch.setattr(jc, "_MAX_EXTRA_CALLS", 0)
    with pytest.raises(jc.JiraError) as err:
        q(client(jira), table="issue_comments", jql="project = OPS")
    assert "Narrow the JQL" in str(err.value)


# ----------------------------------------------------------------------------- reference tables

def test_lookups_and_fields_are_cached_per_identity(jira):
    c = client(jira)
    assert set(q(c, table="statuses")["name"]) == {"To Do", "In Progress", "In Review", "Done"}
    q(c, table="statuses")
    q(c, table="priorities")
    assert [r for r in jira["mock"].REQUEST_LOG if r[1].endswith("/status")].__len__() == 1
    fields = q(c, table="fields")
    assert fields.set_index("id").loc["customfield_10016", "column"] == "story_points"
    assert fields.set_index("id").loc["customfield_10020", "column"] == "sprint"


def test_projects_versions_components_respect_the_scope(jira):
    c = client(jira, "alice", projects="OPS")
    assert list(q(c, table="projects")["key"]) == ["OPS"]
    versions = q(c, table="versions")
    assert set(versions["name"]) == {"1.0", "1.1", "2.0"} and versions["released"].sum() == 2
    assert set(q(c, table="components")["name"]) == {"Database", "Network", "API"}
    with pytest.raises(GlobScopeError):
        q(c, table="versions", project="SEC")


def test_sprints_cover_scrum_boards_in_scope(jira):
    c = client(jira, "alice", projects="OPS, HR")
    sprints = q(c, table="sprints")
    assert set(sprints["id"]) == {41, 42, 43}           # HR's kanban board has no sprints; SEC is out of scope
    assert list(q(c, table="sprints", board_id=1)["state"]).count("closed") == 2
    with pytest.raises(GlobScopeError):
        q(c, table="sprints", board_id=3)
    assert set(q(c, table="boards")["project"]) == {"OPS", "HR"}


def test_a_429_is_retried_then_typed(jira):
    jira["mock"].STATE["rate_limit_next"] = 1
    assert len(q(client(jira), table="issues", jql="project = HR")) == 2
    jira["mock"].STATE["rate_limit_next"] = 10
    with pytest.raises(jc.JiraHTTPError) as err:
        q(client(jira), table="issues", jql="project = HR")
    assert err.value.kind == "throttled"


def test_schema_discovery_is_one_call_for_any_number_of_tables(jira):
    c = client(jira)
    tables = c.get_schemas()
    assert len(tables) == 16 and c.calls == 1
    issues = next(t for t in tables if t.name == "issues")
    assert {"story_points", "key", "sprint"} <= {col.name for col in issues.columns}
    narrowed = client(jira, tables="issues, sprints").get_schemas()
    assert [t.name for t in narrowed] == ["issues", "sprints"]


def test_sprint_strings_from_data_center_are_parsed():
    raw = ["com.atlassian.greenhopper.service.sprint.Sprint@1a[id=41,rapidViewId=1,state=CLOSED,name=Sprint, with comma,"
           "startDate=2026-10-06,endDate=<null>,goal=]"]
    assert jc.parse_sprints(raw) == [{"id": "41", "name": "Sprint, with comma", "state": "closed"}]


# ----------------------------------------------------------------------------- issues as documents

def test_read_file_returns_header_description_and_every_comment(jira):
    doc = client(jira).read_file("issue:OPS-1")
    assert doc.name == "OPS-1.txt" and doc.startswith("# OPS-1: Orders database failover took 22 minutes")
    assert "Status: Done" in doc and "Sprint: OPS Sprint 41" in doc and "| Impact | Highest |" in doc
    assert "## Comments (25)" in doc and "## Attachments" in doc


@pytest.mark.parametrize("ref", ["OPS-6", "ops-6", "issue:OPS-6", "{base}/browse/OPS-6"])
def test_read_file_accepts_keys_and_issue_links(jira, ref):
    doc = client(jira).read_file(ref.format(base=jira["base_url"]))
    assert "relates to OPS-1" in doc


def test_documents_respect_security_and_scope(jira):
    with pytest.raises(jc.JiraHTTPError) as err:
        client(jira, "bob").read_file("OPS-7")
    assert err.value.kind == "not_found"
    with pytest.raises(GlobScopeError):
        client(jira, "alice", projects="OPS").read_file("SEC-1")
    with pytest.raises(GlobScopeError):
        client(jira, "alice", projects="OPS").list_files("SEC")


@pytest.mark.parametrize("ref", ["", "OPS", "https://elsewhere.example.com/browse/OPS-1", "../etc"])
def test_bad_issue_refs_are_refused_without_a_request(jira, ref):
    with pytest.raises(jc.JiraError):
        client(jira).read_file(ref)
    assert jira["mock"].REQUEST_LOG == []


def test_search_and_list_files(jira):
    c = client(jira, projects="OPS")
    hits = c.search_files("failover")
    assert {h["id"] for h in hits} == {"issue:OPS-1", "issue:OPS-2", "issue:OPS-6"}
    assert hits[0]["web_url"].endswith("/browse/" + hits[0]["id"].split(":")[1])
    with pytest.raises(SearchStopped):
        c.search_files("failover", limit=1)
    listed = c.list_files("OPS", limit=5)
    assert len(listed) == 5 and all(e["id"].startswith("issue:OPS-") for e in listed)


def test_documents_can_be_turned_off(jira):
    c = client(jira, include_issue_documents=False)
    assert c.capabilities == {Capability.QUERY}
    with pytest.raises(jc.JiraError):
        c.read_file("OPS-1")


def test_read_raw_bytes_cap(jira):
    with pytest.raises(FileTooLargeError):
        client(jira).read_raw_bytes("OPS-1", max_bytes=10)


def test_prompt_text_states_scope_tables_and_how_to_aggregate(jira):
    c = client(jira, projects="OPS")
    text = c.system_prompt()
    assert "limited to the projects OPS" in text and "issue_changelog" in text and "pandas" in text
    assert "OPS" in jc.planner_scope_note({"projects": "ops"})
