"""Build the RBAC cast for the MCP builder-tool ACL matrix — through the real API.

Used by the e2e suite (``http`` = FastAPI ``TestClient``) and by the live
sandbox runner ``tools/agent/mcp_acl_live.py`` (``http`` = ``httpx.Client``).
Nothing here writes to the database directly: every user, role, grant, agent,
instruction and API key is created by the same routes the web app calls.

Requirements on the server: enterprise custom roles/groups available, and
uninvited sign-ups + multiple organizations allowed (for the outsider). The
e2e suite and the live runner both arrange that.

``invite_token_for(email)`` returns the pending invite token for an invited
email (registration is invite-gated); tests read it from the test DB.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tests.mocks import mcp_acl_matrix as M
from tests.mocks.bow_mcp_client import BowMCPClient

CHINOOK_PATH = (Path(__file__).resolve().parents[1] / "config" / "chinook.sqlite").resolve()
PASSWORD = "Test1234!"


@dataclass
class Persona:
    name: str
    email: str
    token: str
    user_id: str
    org_id: str
    api_key: str = ""

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "X-Organization-Id": str(self.org_id)}


@dataclass
class World:
    http: object
    org_id: str
    personas: dict[str, Persona]
    agents: dict[str, dict]                 # "ALPHA" → data source json
    instructions: dict[str, str] = field(default_factory=dict)  # "inst_alpha" → id
    tag: str = ""

    def mcp(self, persona: str, *, stream: bool = False) -> BowMCPClient:
        return BowMCPClient(self.http, api_key=self.personas[persona].api_key, stream=stream)

    def agent_id(self, key: str) -> str:
        return self.agents[key]["id"]

    def admin(self) -> Persona:
        return self.personas[M.ORG_ADMIN]

    # REST helpers (ground truth for side-effect checks) ──────────────────
    def rest_get_instruction(self, persona: str, inst_id: str):
        return self.http.get(f"/api/instructions/{inst_id}", headers=self.personas[persona].headers())

    def rest_schema(self, persona: str, agent: str):
        return self.http.get(
            f"/api/data_sources/{self.agent_id(agent)}/full_schema",
            headers=self.personas[persona].headers(),
        )

    def truth_reader(self, agents) -> str:
        """Who reads ground truth back over REST. Org admins don't see rows on
        a private agent they aren't a member of (by design), so OWNED is read
        by its owner."""
        return M.AGENT_OWNER if "OWNED" in agents else M.ORG_ADMIN

    def active_tables(self, agent: str) -> set[str]:
        r = self.rest_schema(self.truth_reader([agent]), agent)
        assert r.status_code == 200, r.text
        return {t["name"] for t in r.json() if t.get("is_active")}

    def create_instruction_as(self, persona: str, text: str, agents: list[str]) -> str:
        ds_ids = [self.agent_id(a) for a in agents]
        route = "/api/instructions/global" if not ds_ids or persona in (M.ORG_ADMIN, M.INSTRUCTION_ADMIN) else "/api/instructions"
        r = self.http.post(route, json={
            "text": text, "title": text[:40], "status": "published",
            "category": "general", "data_source_ids": ds_ids,
        }, headers=self.personas[persona].headers())
        assert r.status_code == 200, f"seed instruction as {persona}: {r.status_code} {r.text}"
        return r.json()["id"]


def _ok(resp, what: str):
    assert resp.status_code in (200, 201, 204), f"{what}: {resp.status_code} {resp.text[:500]}"
    return resp.json() if resp.status_code != 204 and resp.text else {}


def _register_login(http, email: str, invite_token: str | None) -> tuple[str, dict]:
    body = {"name": email.split("@")[0], "email": email, "password": PASSWORD}
    if invite_token:
        body["invite_token"] = invite_token
    r = http.post("/api/auth/register", json=body)
    assert r.status_code == 201, f"register {email}: {r.status_code} {r.text}"
    r = http.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200, f"login {email}: {r.text}"
    token = r.json()["access_token"]
    who = _ok(http.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}), "whoami")
    return token, who


def _bootstrap_org_owner(http, email: str) -> Persona:
    token, who = _register_login(http, email, None)
    if who.get("organizations"):
        org_id = who["organizations"][0]["id"]
    else:
        org = _ok(http.post("/api/organizations", json={"name": f"org_{uuid.uuid4().hex[:8]}"},
                            headers={"Authorization": f"Bearer {token}"}), "create org")
        org_id = org["id"]
    return Persona(name="", email=email, token=token, user_id=who["id"], org_id=org_id)


def _enable_mcp(http, p: Persona):
    _ok(http.put("/api/organization/settings", json={"config": {"mcp_enabled": {"value": True}}},
                 headers=p.headers()), "enable mcp")


def _api_key(http, p: Persona) -> str:
    return _ok(http.post("/api/api_keys", json={"name": f"mcp-acl-{p.name}"}, headers=p.headers()),
               f"api key {p.name}")["key"]


def _create_agent(http, p: Persona, name: str, *, is_public: bool) -> dict:
    ds = _ok(http.post("/api/data_sources", json={
        "name": name, "type": "sqlite",
        "config": {"database": str(CHINOOK_PATH)}, "credentials": {},
        "auth_policy": "system_only",
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
    }, headers=p.headers()), f"create agent {name}")
    put = _ok(http.put(f"/api/data_sources/{ds['id']}", json={"is_public": is_public}, headers=p.headers()),
              f"set is_public {name}")
    assert put["is_public"] is is_public
    ds["is_public"] = is_public
    # Agents start with no tables selected; pick a fixed handful so readers
    # have something to see and managers have something to toggle.
    _ok(http.get(f"/api/data_sources/{ds['id']}/refresh_schema", headers=p.headers()), "refresh schema")
    tables = _ok(http.get(f"/api/data_sources/{ds['id']}/full_schema", headers=p.headers()), "schema")
    names = sorted(t["name"] for t in tables)
    assert len(names) >= 4, f"agent {name} has no tables: {names}"
    _ok(http.put(f"/api/data_sources/{ds['id']}/update_tables_status",
                 json={"activate": names[:4], "deactivate": []}, headers=p.headers()), "activate tables")
    ds["seed_tables"] = names[:4]
    return ds


def _connection_id(http, admin: Persona, ds: dict) -> str:
    for src in (ds, _ok(http.get(f"/api/data_sources/{ds['id']}", headers=admin.headers()), "get agent")):
        conns = src.get("connections") or []
        if conns:
            return conns[0]["id"]
    raise AssertionError(f"agent {ds['id']} has no connection")


def build_world(http, invite_token_for: Callable[[str], str | None], *, tag: str = "") -> World:
    tag = tag or uuid.uuid4().hex[:6]
    email = lambda role: f"{role.replace('_', '.')}.{tag}@acl.example.com"  # noqa: E731

    admin = _bootstrap_org_owner(http, email(M.ORG_ADMIN))
    admin.name = M.ORG_ADMIN
    org_id = admin.org_id
    _enable_mcp(http, admin)
    H = admin.headers()

    agents = {
        "ALPHA": _create_agent(http, admin, f"alpha-{tag}", is_public=False),
        "BETA": _create_agent(http, admin, f"beta-{tag}", is_public=False),
        "PUB": _create_agent(http, admin, f"pub-{tag}", is_public=True),
    }

    def role(name, perms):
        return _ok(http.post(f"/api/organizations/{org_id}/roles",
                             json={"name": f"{name}-{tag}", "permissions": perms}, headers=H), f"role {name}")["id"]

    instr_role = role("instruction-admins", ["manage_instructions"])
    creator_role = role("agent-creators", ["create_data_source"])

    personas: dict[str, Persona] = {M.ORG_ADMIN: admin}
    for name in M.IN_ORG:
        if name == M.ORG_ADMIN:
            continue
        mail = email(name)
        _ok(http.post(f"/api/organizations/{org_id}/members",
                      json={"organization_id": org_id, "email": mail, "role": "member"}, headers=H),
            f"invite {name}")
        token, who = _register_login(http, mail, invite_token_for(mail))
        personas[name] = Persona(name=name, email=mail, token=token, user_id=who["id"], org_id=org_id)

    def assign(role_id, principal_type, principal_id):
        _ok(http.post(f"/api/organizations/{org_id}/role-assignments", json={
            "role_id": role_id, "principal_type": principal_type, "principal_id": principal_id,
        }, headers=H), "assign role")

    def grant(resource_type, resource_id, principal_type, principal_id, perms):
        _ok(http.post(f"/api/organizations/{org_id}/resource-grants", json={
            "resource_type": resource_type, "resource_id": resource_id,
            "principal_type": principal_type, "principal_id": principal_id, "permissions": perms,
        }, headers=H), f"grant {perms} on {resource_type}")

    assign(instr_role, "user", personas[M.INSTRUCTION_ADMIN].user_id)
    assign(creator_role, "user", personas[M.AGENT_OWNER].user_id)

    alpha = agents["ALPHA"]["id"]
    grant("data_source", alpha, "user", personas[M.AGENT_MANAGER].user_id, ["manage"])
    grant("data_source", alpha, "user", personas[M.AGENT_INSTR_MANAGER].user_id, ["manage_instructions"])
    grant("data_source", alpha, "user", personas[M.AGENT_EVALS_MANAGER].user_id, ["manage_evals"])
    _ok(http.post(f"/api/data_sources/{alpha}/members",
                  json={"principal_type": "user", "principal_id": personas[M.AGENT_VIEWER].user_id},
                  headers=H), "add viewer")
    grant("connection", _connection_id(http, admin, agents["ALPHA"]), "user",
          personas[M.CONNECTION_MANAGER].user_id, ["manage_data_sources"])
    group = _ok(http.post(f"/api/organizations/{org_id}/groups", json={"name": f"alpha-stewards-{tag}"},
                          headers=H), "group")
    _ok(http.post(f"/api/organizations/{org_id}/groups/{group['id']}/members",
                  json={"user_id": personas[M.GROUP_MANAGER].user_id}, headers=H), "group member")
    grant("data_source", alpha, "group", group["id"], ["manage"])

    # The owner creates their own agent → becomes its manager.
    agents["OWNED"] = _create_agent(http, personas[M.AGENT_OWNER], f"owned-{tag}", is_public=False)

    # Outsider: admin of a separate org.
    outsider = _bootstrap_org_owner(http, email(M.OUTSIDER))
    assert outsider.org_id != org_id, "outsider landed in the main org"
    outsider.name = M.OUTSIDER
    _enable_mcp(http, outsider)
    personas[M.OUTSIDER] = outsider

    for p in personas.values():
        p.api_key = _api_key(http, p)

    world = World(http=http, org_id=org_id, personas=personas, agents=agents, tag=tag)
    for key, scope in M.INSTRUCTION_TARGETS.items():
        author = M.AGENT_OWNER if key == "inst_owned" else M.ORG_ADMIN
        world.instructions[key] = world.create_instruction_as(author, f"seed {key} {tag}", scope)
    return world
