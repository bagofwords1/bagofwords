"""A stand-in for the Microsoft Entra ID provisioning engine, driving a SCIM 2.0
service provider exactly the way Entra does.

It stands in for the IdP boundary (tests/AGENTS.md rule 4): everything on the
BOW side runs for real. The request shapes are the ones Microsoft documents for
non-gallery SCIM apps ("Develop a SCIM endpoint for user provisioning to apps
from Microsoft Entra ID", Users/Groups operations), and the engine follows the
same rules Entra's does:

* **Test connection** — ``GET /Users?filter=userName eq "<random guid>"`` must
  answer 200 with zero results.
* **Users before groups**, so group member references resolve.
* **Matching**: an object Entra has not provisioned yet is first looked up by
  its matching attribute (``userName`` = UPN for users, ``displayName`` for
  groups, with ``excludedAttributes=members``). One hit → the target id is
  escrowed (adopted); zero → ``POST``; more than one → the object fails with
  a "multiple matches" error, as Entra does.
* **Groups are created without members**, then members are added with
  ``PATCH {"op": "Add", "path": "members", "value": [{"$ref": null, "value": id}]}``
  and removed with ``"op": "Remove"`` in the same shape. Renames are
  ``{"op": "Replace", "path": "displayName"}``. Only members that Entra itself
  provisioned (in scope, escrowed) are ever sent.
* **Out of scope**: a user who is unassigned (or disabled in the directory) is
  soft-deleted with ``PATCH active=false``; a group that is unassigned or
  deleted is ``DELETE``d.
* Ops are capitalised (``Add``/``Replace``/``Remove``) and bodies go out as
  ``application/scim+json``. ``legacy_patch=True`` reproduces apps without the
  ``aadOptscim062020`` flag, which send booleans as strings (``"False"``).
* Every response is validated the way Entra consumes it: list responses need
  ``totalResults``/``Resources``, creates must return an ``id``, success is any
  2xx (Entra takes 200 or 204 for PATCH/DELETE).

Failures do not abort a cycle: like Entra, the engine records them per object
(``CycleReport.errors``) and carries on. ``wire_log`` keeps every exchange
(without the Authorization header) so a run can be inspected or saved as
fixtures.

Use it with any httpx-compatible client — FastAPI's ``TestClient`` in tests,
or ``httpx.Client(base_url=...)`` against a running stack::

    uv run python tests/mocks/entra_scim_provisioner.py \\
        --base-url http://localhost:8000 --token "$BOW_SCIM_TOKEN"

(the standalone run executes a self-checking create → change → unassign
scenario; see ``main``).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_ENTERPRISE_USER = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
SCIM_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
MS_ADSCIM_GROUP = "http://schemas.microsoft.com/2006/11/ResourceManagement/ADSCIM/2.0/Group"
PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
LIST_RESPONSE = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_CONTENT_TYPES = ("application/scim+json", "application/json")


# ── Directory (the Entra tenant) ─────────────────────────────────────────


@dataclass
class DirectoryUser:
    object_id: str
    user_principal_name: str
    mail: str
    given_name: str
    surname: str
    display_name: str
    mail_nickname: str
    account_enabled: bool = True


@dataclass
class DirectoryGroup:
    object_id: str
    display_name: str
    members: Set[str] = field(default_factory=set)  # user object ids


class EntraTenant:
    """The source directory: users, groups and their memberships."""

    def __init__(self, domain: str = "contoso.onmicrosoft.com"):
        self.domain = domain
        self.users: Dict[str, DirectoryUser] = {}
        self.groups: Dict[str, DirectoryGroup] = {}

    def add_user(self, given_name: str, surname: str, *, mail: Optional[str] = None,
                 upn: Optional[str] = None) -> DirectoryUser:
        nickname = f"{given_name}.{surname}".lower()
        mail = mail or f"{nickname}@{self.domain}"
        user = DirectoryUser(
            object_id=str(uuid.uuid4()),
            user_principal_name=upn or mail,
            mail=mail,
            given_name=given_name,
            surname=surname,
            display_name=f"{given_name} {surname}",
            mail_nickname=nickname,
        )
        self.users[user.object_id] = user
        return user

    def add_group(self, display_name: str, members: List[DirectoryUser] = ()) -> DirectoryGroup:
        group = DirectoryGroup(object_id=str(uuid.uuid4()), display_name=display_name,
                               members={u.object_id for u in members})
        self.groups[group.object_id] = group
        return group

    def delete_group(self, group: DirectoryGroup) -> None:
        self.groups.pop(group.object_id, None)


# ── Provisioning engine ──────────────────────────────────────────────────


class EntraProvisioningError(Exception):
    def __init__(self, object_id: str, step: str, status: Optional[int], detail: Any):
        super().__init__(f"{step} for {object_id} failed ({status}): {detail}")
        self.object_id = object_id
        self.step = step
        self.status = status
        self.detail = detail


@dataclass
class CycleReport:
    users_created: int = 0
    users_matched: int = 0
    users_updated: int = 0
    users_disabled: int = 0
    groups_created: int = 0
    groups_matched: int = 0
    groups_updated: int = 0
    groups_deleted: int = 0
    members_added: int = 0
    members_removed: int = 0
    errors: List[EntraProvisioningError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class EntraProvisioningApp:
    """An enterprise application with automatic provisioning to a SCIM endpoint.

    ``http`` is any httpx-compatible client (``TestClient``/``httpx.Client``)
    rooted at the service provider's host; ``tenant_url`` is the path Entra is
    configured with (``/scim/v2``).
    """

    def __init__(self, tenant: EntraTenant, http, token: str, *,
                 tenant_url: str = "/scim/v2", legacy_patch: bool = False):
        self.tenant = tenant
        self.http = http
        self.token = token
        self.tenant_url = tenant_url.rstrip("/")
        self.legacy_patch = legacy_patch
        self.assigned_groups: Set[str] = set()
        self.assigned_users: Set[str] = set()
        # Escrow: directory object id → target (BOW) id.
        self.user_ids: Dict[str, str] = {}
        self.group_ids: Dict[str, str] = {}
        # Last state Entra pushed, so later cycles only send deltas.
        self.user_active: Dict[str, bool] = {}
        self.group_members: Dict[str, Set[str]] = {}
        self.wire_log: List[Dict[str, Any]] = []

    # -- assignment --

    def assign_group(self, group: DirectoryGroup) -> None:
        self.assigned_groups.add(group.object_id)

    def unassign_group(self, group: DirectoryGroup) -> None:
        self.assigned_groups.discard(group.object_id)

    def assign_user(self, user: DirectoryUser) -> None:
        self.assigned_users.add(user.object_id)

    # -- wire --

    def _request(self, method: str, path: str, body: Optional[dict] = None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/scim+json"}
        content = None
        if body is not None:
            headers["Content-Type"] = "application/scim+json; charset=utf-8"
            content = json.dumps(body)
        response = self.http.request(method, f"{self.tenant_url}{path}", content=content, headers=headers)
        try:
            parsed = response.json() if response.content else None
        except ValueError:
            parsed = response.text
        self.wire_log.append({
            "method": method,
            "path": f"{self.tenant_url}{path}",
            "request": body,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "response": parsed,
        })
        return response, parsed

    def _expect_2xx(self, object_id: str, step: str, response, parsed) -> None:
        if not 200 <= response.status_code < 300:
            raise EntraProvisioningError(object_id, step, response.status_code, parsed)
        if response.content:
            ctype = (response.headers.get("content-type") or "").split(";")[0].strip()
            if ctype not in SCIM_CONTENT_TYPES:
                raise EntraProvisioningError(object_id, step, response.status_code,
                                             f"unexpected content-type {ctype!r}")

    def _query_one(self, object_id: str, step: str, path: str) -> Optional[dict]:
        response, parsed = self._request("GET", path)
        self._expect_2xx(object_id, step, response, parsed)
        if not isinstance(parsed, dict) or LIST_RESPONSE not in parsed.get("schemas", []):
            raise EntraProvisioningError(object_id, step, response.status_code, "not a ListResponse")
        total = parsed.get("totalResults")
        resources = parsed.get("Resources") or []
        if not isinstance(total, int):
            raise EntraProvisioningError(object_id, step, response.status_code, "totalResults missing")
        if total > 1 or len(resources) > 1:
            raise EntraProvisioningError(object_id, step, response.status_code,
                                         f"multiple matches ({total}) for the matching attribute")
        if total == 0:
            if resources:
                raise EntraProvisioningError(object_id, step, response.status_code,
                                             "totalResults is 0 but Resources is not empty")
            return None
        resource = resources[0] if resources else None
        if not resource or not resource.get("id"):
            raise EntraProvisioningError(object_id, step, response.status_code, "match has no id")
        return resource

    @staticmethod
    def _eq(attr: str, value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return quote(f'{attr} eq "{escaped}"', safe="")

    def _bool(self, value: bool):
        return ("True" if value else "False") if self.legacy_patch else value

    def _patch(self, object_id: str, step: str, path: str, operations: List[dict]) -> None:
        response, parsed = self._request("PATCH", path, {"schemas": [PATCH_OP], "Operations": operations})
        self._expect_2xx(object_id, step, response, parsed)

    # -- connection test (the "Test Connection" button) --

    def test_connection(self) -> None:
        probe = f"Test_User_{uuid.uuid4()}"
        if self._query_one("test-connection", "TestConnection", f"/Users?filter={self._eq('userName', probe)}"):
            raise EntraProvisioningError("test-connection", "TestConnection", 200,
                                         "a random userName matched a user")

    # -- scope --

    def _users_in_scope(self) -> Set[str]:
        scope = set(self.assigned_users)
        for gid in self.assigned_groups:
            group = self.tenant.groups.get(gid)
            if group:
                scope |= group.members
        return {
            oid for oid in scope
            if oid in self.tenant.users and self.tenant.users[oid].account_enabled
        }

    def _groups_in_scope(self) -> Set[str]:
        return {gid for gid in self.assigned_groups if gid in self.tenant.groups}

    # -- users --

    def _user_body(self, user: DirectoryUser) -> dict:
        return {
            "schemas": [SCIM_USER, SCIM_ENTERPRISE_USER],
            "externalId": user.mail_nickname,
            "userName": user.user_principal_name,
            "active": user.account_enabled,
            "displayName": user.display_name,
            "emails": [{"primary": True, "type": "work", "value": user.mail}],
            "meta": {"resourceType": "User"},
            "name": {
                "formatted": user.display_name,
                "familyName": user.surname,
                "givenName": user.given_name,
            },
            "roles": [],
        }

    def _provision_user(self, oid: str, report: CycleReport) -> None:
        user = self.tenant.users[oid]
        target_id = self.user_ids.get(oid)
        if target_id is None:
            match = self._query_one(oid, "GetUser",
                                    f"/Users?filter={self._eq('userName', user.user_principal_name)}")
            if match is None:
                response, parsed = self._request("POST", "/Users", self._user_body(user))
                self._expect_2xx(oid, "CreateUser", response, parsed)
                if not isinstance(parsed, dict) or not parsed.get("id"):
                    raise EntraProvisioningError(oid, "CreateUser", response.status_code, "no id returned")
                self.user_ids[oid] = parsed["id"]
                self.user_active[oid] = True
                report.users_created += 1
                return
            self.user_ids[oid] = match["id"]
            report.users_matched += 1
            current = match
        else:
            response, current = self._request("GET", f"/Users/{target_id}")
            if response.status_code == 404:
                # Deleted on the target side: forget it and start over.
                self.user_ids.pop(oid, None)
                self._provision_user(oid, report)
                return
            self._expect_2xx(oid, "GetUser", response, current)

        operations = []
        if current.get("active") is not True:
            operations.append({"op": "Replace", "path": "active", "value": self._bool(True)})
        if current.get("displayName") != user.display_name:
            operations.append({"op": "Replace", "path": "displayName", "value": user.display_name})
        if operations:
            self._patch(oid, "UpdateUser", f"/Users/{self.user_ids[oid]}", operations)
            report.users_updated += 1
        self.user_active[oid] = True

    def _disable_user(self, oid: str, report: CycleReport) -> None:
        self._patch(oid, "DisableUser", f"/Users/{self.user_ids[oid]}",
                    [{"op": "Replace", "path": "active", "value": self._bool(False)}])
        self.user_active[oid] = False
        report.users_disabled += 1

    # -- groups --

    def _provision_group(self, gid: str, report: CycleReport) -> None:
        group = self.tenant.groups[gid]
        target_id = self.group_ids.get(gid)
        if target_id is None:
            match = self._query_one(
                gid, "GetGroup",
                f"/Groups?excludedAttributes=members&filter={self._eq('displayName', group.display_name)}",
            )
            if match is None:
                response, parsed = self._request("POST", "/Groups", {
                    "schemas": [SCIM_GROUP, MS_ADSCIM_GROUP],
                    "externalId": group.object_id,
                    "displayName": group.display_name,
                    "meta": {"resourceType": "Group"},
                })
                self._expect_2xx(gid, "CreateGroup", response, parsed)
                if not isinstance(parsed, dict) or not parsed.get("id"):
                    raise EntraProvisioningError(gid, "CreateGroup", response.status_code, "no id returned")
                self.group_ids[gid] = parsed["id"]
                report.groups_created += 1
            else:
                self.group_ids[gid] = match["id"]
                report.groups_matched += 1
            self.group_members[gid] = set()
        else:
            response, current = self._request("GET", f"/Groups/{target_id}?excludedAttributes=members")
            if response.status_code == 404:
                self.group_ids.pop(gid, None)
                self.group_members.pop(gid, None)
                self._provision_group(gid, report)
                return
            self._expect_2xx(gid, "GetGroup", response, current)
            if current.get("displayName") != group.display_name:
                self._patch(gid, "UpdateGroup", f"/Groups/{target_id}",
                            [{"op": "Replace", "path": "displayName", "value": group.display_name}])
                report.groups_updated += 1

        target_id = self.group_ids[gid]
        provisioned = {oid for oid, active in self.user_active.items() if active}
        desired = {self.user_ids[oid] for oid in group.members if oid in provisioned}
        synced = self.group_members.get(gid, set())
        to_add, to_remove = sorted(desired - synced), sorted(synced - desired)
        if to_add:
            self._patch(gid, "AddMembers", f"/Groups/{target_id}", [
                {"op": "Add", "path": "members", "value": [{"$ref": None, "value": v} for v in to_add]}
            ])
            report.members_added += len(to_add)
        if to_remove:
            self._patch(gid, "RemoveMembers", f"/Groups/{target_id}", [
                {"op": "Remove", "path": "members", "value": [{"$ref": None, "value": v} for v in to_remove]}
            ])
            report.members_removed += len(to_remove)
        self.group_members[gid] = desired

    def _delete_group(self, gid: str, report: CycleReport) -> None:
        response, parsed = self._request("DELETE", f"/Groups/{self.group_ids[gid]}")
        if response.status_code != 404:  # already gone counts as done
            self._expect_2xx(gid, "DeleteGroup", response, parsed)
        self.group_ids.pop(gid, None)
        self.group_members.pop(gid, None)
        report.groups_deleted += 1

    # -- cycles --

    def run_cycle(self) -> CycleReport:
        """One provisioning cycle over everything assigned to the app."""
        report = CycleReport()
        users_in_scope = self._users_in_scope()
        for oid in sorted(users_in_scope):
            self._attempt(report, self._provision_user, oid)
        for oid in sorted(set(self.user_ids) - users_in_scope):
            if self.user_active.get(oid):
                self._attempt(report, self._disable_user, oid)

        groups_in_scope = self._groups_in_scope()
        for gid in sorted(groups_in_scope):
            self._attempt(report, self._provision_group, gid)
        for gid in sorted(set(self.group_ids) - groups_in_scope):
            self._attempt(report, self._delete_group, gid)
        return report

    def provision_on_demand(self, group: DirectoryGroup) -> CycleReport:
        """Provision one group and its members now (Entra's "Provision on demand")."""
        report = CycleReport()
        for oid in sorted(group.members & self._users_in_scope()):
            self._attempt(report, self._provision_user, oid)
        if group.object_id in self._groups_in_scope():
            self._attempt(report, self._provision_group, group.object_id)
        return report

    @staticmethod
    def _attempt(report: CycleReport, step, object_id: str) -> None:
        try:
            step(object_id, report)
        except EntraProvisioningError as exc:
            report.errors.append(exc)


# ── Standalone run against a live stack ─────────────────────────────────


def _check(label: str, condition: bool, detail: Any = "") -> bool:
    print(("PASS" if condition else "FAIL"), "-", label, "" if condition else f"  >> {detail}")
    return bool(condition)


def main() -> int:
    import argparse

    import httpx

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", required=True, help="a bow_scim_ token for the target org")
    parser.add_argument("--legacy-patch", action="store_true",
                        help="send booleans as strings, like Entra apps without aadOptscim062020")
    parser.add_argument("--wire-log", help="write every request/response to this JSON file")
    args = parser.parse_args()

    run = uuid.uuid4().hex[:6]
    tenant = EntraTenant(domain=f"entra-sim-{run}.example.com")
    alice = tenant.add_user("Alice", "Analyst")
    bob = tenant.add_user("Bob", "Builder")
    carol = tenant.add_user("Carol", "Chen")
    team = tenant.add_group(f"Data Team {run}", [alice, bob])

    results: List[bool] = []
    with httpx.Client(base_url=args.base_url, timeout=30) as http:
        app = EntraProvisioningApp(tenant, http, args.token, legacy_patch=args.legacy_patch)

        def group_state() -> dict:
            response = http.get(f"/scim/v2/Groups/{app.group_ids[team.object_id]}",
                                headers={"Authorization": f"Bearer {args.token}"})
            return response.json()

        def members_of(state: dict) -> Set[str]:
            return {m["value"] for m in state.get("members", [])}

        try:
            app.test_connection()
            results.append(_check("test connection", True))
        except EntraProvisioningError as exc:
            results.append(_check("test connection", False, exc))

        app.assign_group(team)
        report = app.run_cycle()
        results.append(_check("initial cycle has no errors", report.ok, report.errors))
        state = group_state()
        results.append(_check("group created with both members",
                              members_of(state) == {app.user_ids[alice.object_id], app.user_ids[bob.object_id]},
                              state))

        tenant.groups[team.object_id].members = {bob.object_id, carol.object_id}
        team.display_name = f"Data Platform {run}"
        report = app.run_cycle()
        results.append(_check("change cycle has no errors", report.ok, report.errors))
        state = group_state()
        results.append(_check("rename applied", state.get("displayName") == team.display_name, state))
        results.append(_check("membership follows the directory",
                              members_of(state) == {app.user_ids[bob.object_id], app.user_ids[carol.object_id]},
                              state))
        alice_state = http.get(f"/scim/v2/Users/{app.user_ids[alice.object_id]}",
                               headers={"Authorization": f"Bearer {args.token}"}).json()
        results.append(_check("user leaving scope is deactivated", alice_state.get("active") is False, alice_state))

        target_id = app.group_ids[team.object_id]
        app.unassign_group(team)
        report = app.run_cycle()
        results.append(_check("unassign cycle has no errors", report.ok, report.errors))
        gone = http.get(f"/scim/v2/Groups/{target_id}", headers={"Authorization": f"Bearer {args.token}"})
        results.append(_check("unassigned group is deleted", gone.status_code == 404, gone.text))

    if args.wire_log:
        with open(args.wire_log, "w") as fh:
            json.dump(app.wire_log, fh, indent=2, default=str)
        print(f"wire log: {args.wire_log} ({len(app.wire_log)} exchanges)")
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
