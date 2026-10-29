"""The people of the corpus, created through the real API.

Users, the org, groups, roles, projects and data sources live in tables this
project does not change, so they are created the ordinary way (tests/AGENTS.md
rule 5) before the database is taken back to `MAIN_BASE_REVISION`.

Each role exists because the access checks treat it differently -- see the
ROLES table for the code path that sets it apart.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tests.fixtures.dashboard_corpus.rows import Rows

# role key -> what makes it a distinct reader (the access path it exercises)
ROLES: dict[str, str] = {
    "owner": "owns every corpus conversation; passes every check, sees all data",
    "admin": "org full admin; no bypass on /r, bypass on /api/reports/{id}",
    "member": "plain member; passes 'internal', refused on 'shared'",
    "direct_share": "named in report_shares; may also run as the creator",
    "group_share": "reaches 'shared' through a group; /api/artifacts refuses groups",
    "group_removed": "was in the shared group, membership removed",
    "proj_owner": "owns the projects, not the conversations; project bypass",
    "proj_viewer": "user grant 'view' on the private project",
    "proj_group": "reaches the private project through a group grant",
    "proj_revoked": "project grant revoked",
    "no_creds": "member reading user-scoped data with no run of their own",
    "has_results": "member with their own step_user_results rows",
    "bow_scoped": "holds manage_settings: may read run-history (bow) reports",
    "bow_unscoped": "plain member on a bow report: refused even when 'internal'",
    "other_org": "admin of another org",
    "service_account": "API-key principal without a Membership",
    "chat_viewer": "owns viewer chat threads on a dashboard",
    "forker": "owns forks of corpus conversations",
    "anonymous": "no credentials",
}


@dataclass
class Person:
    key: str
    user_id: str | None = None
    email: str | None = None
    token: str | None = None
    api_key: str | None = None
    membership_id: str | None = None

    def headers(self, org_id: str | None) -> dict:
        if self.api_key:
            h = {"X-API-Key": self.api_key}
        elif self.token:
            h = {"Authorization": f"Bearer {self.token}"}
        else:
            return {}
        if org_id:
            h["X-Organization-Id"] = str(org_id)
        return h


@dataclass
class Cast:
    org_id: str
    other_org_id: str
    people: dict[str, Person]
    groups: dict[str, str] = field(default_factory=dict)
    projects: dict[str, str] = field(default_factory=dict)
    data_sources: dict[str, str] = field(default_factory=dict)

    def id(self, key: str) -> str:
        return self.people[key].user_id


def _h(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _ok(resp, *codes):
    codes = codes or (200,)
    assert resp.status_code in codes, f"{resp.request.method} {resp.request.url}: {resp.status_code} {resp.text}"
    return resp.json() if resp.content else None


PASSWORD = "Corpus1234!"


def build_cast(
    *,
    test_client,
    create_user,
    login_user,
    whoami,
    sqlite_data_source,
    create_project,
    update_project,
    upsert_project_member,
    remove_project_member,
    delete_project,
    rows: Rows,
) -> Cast:
    # Names and emails derive from the role key, so two builds of the corpus
    # produce the same golden files.
    def signup(key: str) -> Person:
        email = f"{key.replace('_', '.')}@corpus.com"
        create_user(name=f"corpus {key}", email=email, password=PASSWORD)
        token = login_user(email, PASSWORD)
        return Person(key, whoami(token)["id"], email, token)

    def own_org(person: Person, name: str) -> str:
        info = whoami(person.token)
        if info.get("organizations"):
            return info["organizations"][0]["id"]
        return _ok(test_client.post("/api/organizations", json={"name": name},
                                    headers={"Authorization": f"Bearer {person.token}"}))["id"]

    admin = signup("admin")
    org_id = own_org(admin, "ארגון הקורפוס")
    admin_tok = admin.token
    people: dict[str, Person] = {"admin": admin}

    def invite(key: str) -> Person:
        email = f"{key.replace('_', '.')}@corpus.com"
        membership = _ok(test_client.post(
            f"/api/organizations/{org_id}/members",
            json={"organization_id": org_id, "email": email, "role": "member"},
            headers=_h(admin_tok, org_id)))
        p = signup(key)
        p.membership_id = membership["id"]
        people[key] = p
        return p

    for key in ("owner", "member", "direct_share", "group_share", "group_removed",
                "proj_owner", "proj_viewer", "proj_group", "proj_revoked",
                "no_creds", "has_results", "bow_scoped", "bow_unscoped",
                "chat_viewer", "forker"):
        invite(key)

    people["other_org"] = signup("other_org")
    other_org_id = own_org(people["other_org"], "ארגון אחר")
    people["anonymous"] = Person("anonymous")

    # Groups (enterprise-gated routes; the caller activates `enterprise_license`).
    def group(name: str, members: list[str]) -> str:
        g = _ok(test_client.post(f"/api/organizations/{org_id}/groups",
                                 json={"name": name}, headers=_h(admin_tok, org_id)))
        for key in members:
            _ok(test_client.post(f"/api/organizations/{org_id}/groups/{g['id']}/members",
                                 json={"user_id": people[key].user_id},
                                 headers=_h(admin_tok, org_id)), 200, 201)
        return g["id"]

    groups = {
        "analysts": group("אנליסטים", ["group_share", "proj_group"]),
        "former": group("לשעבר", ["group_removed"]),
    }
    _ok(test_client.delete(
        f"/api/organizations/{org_id}/groups/{groups['former']}/members/{people['group_removed'].user_id}",
        headers=_h(admin_tok, org_id)), 200, 204)
    # group_removed also loses the analysts share path: they are added there and
    # removed again, so a stale membership row exists for the shared group.
    _ok(test_client.post(f"/api/organizations/{org_id}/groups/{groups['analysts']}/members",
                         json={"user_id": people["group_removed"].user_id},
                         headers=_h(admin_tok, org_id)), 200, 201)
    _ok(test_client.delete(
        f"/api/organizations/{org_id}/groups/{groups['analysts']}/members/{people['group_removed'].user_id}",
        headers=_h(admin_tok, org_id)), 200, 204)

    # A custom role that may read run-history (bow) reports. (No role can
    # withhold view_reports from a member: it is baseline for every
    # membership -- permission_resolver.BASELINE_PERMISSIONS.)
    def role(name: str, permissions: list[str], key: str) -> None:
        r = _ok(test_client.post(f"/api/organizations/{org_id}/roles",
                                 json={"name": name, "permissions": permissions},
                                 headers=_h(admin_tok, org_id)))
        _ok(test_client.post(f"/api/organizations/{org_id}/role-assignments",
                             json={"role_id": r["id"], "principal_type": "user",
                                   "principal_id": people[key].user_id},
                             headers=_h(admin_tok, org_id)))

    role("ניטור", ["manage_settings"], "bow_scoped")

    # Service account with an API key.
    sa = _ok(test_client.post("/api/service_accounts", json={"name": "סוכן אוטומציה"},
                              headers=_h(admin_tok, org_id)))
    key = _ok(test_client.post(f"/api/service_accounts/{sa['id']}/keys", json={"name": "corpus"},
                               headers=_h(admin_tok, org_id)))
    people["service_account"] = Person("service_account", sa.get("user_id") or sa["id"],
                                       api_key=key.get("key") or key.get("api_key"))

    # Data sources: the bundled chinook sample, public to the org.
    chinook = sqlite_data_source(name="חנות מוזיקה", user_token=admin_tok, org_id=org_id, is_public=True)
    data_sources = {"chinook": chinook["id"]}

    # Projects, owned by proj_owner.
    po = people["proj_owner"]
    projects = {}
    projects["private"] = create_project(name="פרויקט פרטי", user_token=po.token, org_id=org_id)["id"]
    upsert_project_member(projects["private"], people["proj_viewer"].user_id,
                          user_token=po.token, org_id=org_id)
    upsert_project_member(projects["private"], people["proj_revoked"].user_id,
                          user_token=po.token, org_id=org_id)
    remove_project_member(projects["private"], people["proj_revoked"].user_id,
                          user_token=po.token, org_id=org_id)
    # A group grant on a project has no route (resource-grants takes data
    # sources and connections), so it is written directly.
    rows.insert("resource_grants", organization_id=org_id, resource_type="project",
                resource_id=projects["private"], principal_type="group",
                principal_id=groups["analysts"], permissions=["view"])

    projects["org_wide"] = create_project(name="פרויקט ארגוני", user_token=po.token, org_id=org_id)["id"]
    update_project(projects["org_wide"], user_token=po.token, org_id=org_id, access="org")

    projects["deleted"] = create_project(name="פרויקט שנמחק", user_token=po.token, org_id=org_id)["id"]
    upsert_project_member(projects["deleted"], people["proj_viewer"].user_id,
                          user_token=po.token, org_id=org_id)
    delete_project(projects["deleted"], user_token=po.token, org_id=org_id)

    return Cast(org_id=org_id, other_org_id=other_org_id, people=people,
                groups=groups, projects=projects, data_sources=data_sources)
