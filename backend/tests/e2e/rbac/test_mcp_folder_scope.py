"""MCP clients scope reports to agent folders, and folder-shared instructions
apply exactly where they are attached.

Contract:
- browse_agents lists the folders and agents the caller may use, with ids and
  visible paths. A direct grant on one agent reveals neither its private
  parent folder nor its siblings.
- create_report takes agent_folder_ids and/or data_source_ids, persists the
  folders, and still works with only a title. get_context returns the selected
  scope and the agents it resolves to. Losing access to a report's folder never
  widens the report to every agent.
- One instruction can be shared across several folders and agents. get_context
  and read_instruction(report_id) agree on where it applies: in a report whose
  scope reaches one of its targets, and nowhere else (an empty agent list is
  not "global"; `scope` says which it is).
- Editing text keeps every placement. Detaching one target keeps the rest.
  Detaching the last target is refused (never silently global, never left
  applying nowhere); making a rule global is an explicit `scope: "global"` that
  needs org-level instruction admin.
- Authority over a shared rule is authority over every target it touches:
  a manager of one target can't edit it; a curator (custom folder role with
  manage_instructions) can, but their change waits for review.
"""

import json
import uuid

import pytest


def _h(actor, org):
    return {"Authorization": "Bearer " + actor["token"], "X-Organization-Id": str(org)}


class World:
    def __init__(self, test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source,
                 enable_mcp, create_api_key):
        self.http = test_client
        self.admin = bootstrap_admin("admin")
        self.org = self.admin["org_id"]
        enable_mcp(user_token=self.admin["token"], org_id=self.org)
        self._invite = lambda: invite_user_to_org(org_id=self.org, admin_token=self.admin["token"])
        self._key = lambda actor: create_api_key(user_token=actor["token"], org_id=self.org)["key"]
        self._ds = lambda name: sqlite_data_source(name=name, user_token=self.admin["token"], org_id=self.org)
        self.admin["key"] = self._key(self.admin)

    def api(self, actor, method, path, body=None, ok=True):
        r = self.http.request(method, "/api/" + path, json=body, headers=_h(actor, self.org))
        if ok:
            assert r.is_success, f"{method} {path}: {r.status_code} {r.text}"
        return r

    def user(self):
        u = self._invite()
        u["key"] = self._key(u)
        return u

    def folder(self, name, parent=None):
        body = {"name": f"{name} {uuid.uuid4().hex[:4]}"}
        if parent:
            body["parent_id"] = parent
        return self.api(self.admin, "POST", "folders", body).json()["id"]

    def agent(self, name, folder=None):
        ds = self._ds(f"{name} {uuid.uuid4().hex[:4]}")
        if folder:
            self.api(self.admin, "PUT", f"folders/items/agent/{ds['id']}/placement", {"parent_id": folder})
        return ds["id"]

    def share(self, folder, actor, role):
        self.api(self.admin, "PUT", f"folders/{folder}/share",
                 {"principal_type": "user", "principal_id": actor["user_id"], "role": role})

    def grant_role(self, actor, grants):
        role = self.api(self.admin, "POST", f"organizations/{self.org}/roles",
                        {"name": f"role {uuid.uuid4().hex[:6]}", "resource_grants": grants}).json()["id"]
        self.api(self.admin, "POST", f"organizations/{self.org}/role-assignments",
                 {"role_id": role, "principal_type": "user", "principal_id": actor["user_id"]})

    def mcp(self, actor, tool, args):
        r = self.http.post("/api/mcp", headers={"X-API-Key": actor["key"]}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return {"_error": text}
        return json.loads(text)


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, enable_mcp, create_api_key):
    return World(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, enable_mcp, create_api_key)


def _listed(world, actor, report_id):
    out = world.mcp(actor, "get_context", {"report_id": report_id})
    assert "_error" not in out, out
    return {i["id"] for i in out["instructions"]}


def _readable(world, actor, report_id, inst_id):
    out = world.mcp(actor, "read_instruction", {"instruction_id": inst_id, "report_id": report_id})
    return bool(out.get("success"))


def _report(world, actor, *, folders=(), agents=()):
    out = world.mcp(actor, "create_report", {"title": "scope check", "agent_folder_ids": list(folders),
                                             "data_source_ids": list(agents)})
    assert "_error" not in out, out
    return out["report_id"]


@pytest.mark.e2e
def test_folder_rule_is_not_readable_from_a_report_outside_its_folder(world):
    """The reported bug: a rule attached only to one folder has no agents, and
    read_instruction(report_id) used to treat "no agents" as global."""
    disputes, authorization = world.folder("Disputes"), world.folder("Authorization")
    world.agent("disputes agent", disputes)
    world.agent("auth agent", authorization)
    analyst = world.user()
    for f in (disputes, authorization):
        world.share(f, analyst, "Viewer")
    rule = world.api(world.admin, "POST", "instructions", {
        "text": "Disputes: count a chargeback once, on its first notice date.", "status": "published",
        "folder_ids": [disputes], "data_source_ids": [],
    }).json()["id"]

    in_folder = _report(world, analyst, folders=[disputes])
    outside = _report(world, analyst, folders=[authorization])

    assert rule in _listed(world, analyst, in_folder)
    assert _readable(world, analyst, in_folder, rule)
    assert rule not in _listed(world, analyst, outside)
    assert not _readable(world, analyst, outside, rule), "read_instruction disagrees with get_context"


def _matrix(world, personas, reports, inst_id):
    """{(persona, report): (listed by get_context, opened by read_instruction)}."""
    out = {}
    for who, actor in personas.items():
        for where, rid in reports[who].items():
            out[(who, where)] = (inst_id in _listed(world, actor, rid), _readable(world, actor, rid, inst_id))
    return out


def _assert_applies(world, personas, reports, inst_id, expected_where):
    got = _matrix(world, personas, reports, inst_id)
    wrong = {k: v for k, v in got.items() if v != ((k[1] in expected_where),) * 2}
    assert not wrong, f"expected the rule only in {sorted(expected_where)}; mismatches (listed, readable): {wrong}"


def _detail(world, actor, inst_id):
    out = world.mcp(actor, "read_instruction", {"instruction_id": inst_id})
    assert out.get("success"), out
    return out["instruction"]


@pytest.mark.e2e
def test_rule_shared_across_folders_and_an_agent_lifecycle(world):
    disputes, authorization = world.folder("Disputes"), world.folder("Authorization")
    refunds_home, other = world.folder("Refunds"), world.folder("Marketing")
    world.agent("disputes ledger", disputes)
    world.agent("auth events", authorization)
    refunds = world.agent("refunds", refunds_home)
    world.agent("campaigns", other)

    query_user, curator, manager_one, manager_all = (world.user() for _ in range(4))
    for f in (disputes, authorization, refunds_home, other):
        world.share(f, query_user, "Viewer")
        world.share(f, manager_one, "Manager" if f == disputes else "Viewer")
        world.share(f, manager_all, "Manager" if f != other else "Viewer")
    for f in (refunds_home, other):
        world.share(f, curator, "Viewer")
    world.grant_role(curator, [
        {"resource_type": "folder", "resource_id": disputes, "permissions": ["view", "manage_instructions"]},
        {"resource_type": "folder", "resource_id": authorization, "permissions": ["view", "manage_instructions"]},
        {"resource_type": "data_source", "resource_id": refunds, "permissions": ["manage_instructions"]},
    ])
    personas = {"query_only": query_user, "curator": curator, "manager_one": manager_one, "manager_all": manager_all}
    reports = {
        who: {
            "disputes": _report(world, actor, folders=[disputes]),
            "authorization": _report(world, actor, folders=[authorization]),
            "refunds": _report(world, actor, agents=[refunds]),
            "other": _report(world, actor, folders=[other]),
        }
        for who, actor in personas.items()
    }

    # ── create: one rule, two folders + one agent ──
    text = f"Amounts are in cents; divide by 100 before showing money. ref {uuid.uuid4().hex[:6]}"
    made = world.mcp(manager_all, "create_instruction", {
        "text": text, "folder_ids": [disputes, authorization], "data_source_ids": [refunds],
    })
    assert made.get("success") and made["scope"] == "targeted" and not made["requires_approval"], made
    rule = made["instruction_id"]
    seen = _detail(world, manager_all, rule)
    assert seen["scope"] == "targeted"
    assert {f["id"] for f in seen["folders"]} == {disputes, authorization}
    assert [a["id"] for a in seen["agents"]] == [refunds]
    _assert_applies(world, personas, reports, rule, {"disputes", "authorization", "refunds"})

    # ── a manager of one target can't change a rule that also lives elsewhere ──
    for change in ({"old_text": "Amounts are in cents", "text": "Amounts are in dollars"},
                   {"remove_folder_ids": [disputes]}):
        res = world.mcp(manager_one, "edit_instruction", {"instruction_id": rule, **change})
        assert not res.get("success") and res["rejected_reason"] == "permission_denied", res
    assert _detail(world, manager_all, rule)["text"] == text

    # ── a curator may edit, but the change waits for review; readers keep the approved text ──
    res = world.mcp(curator, "edit_instruction", {
        "instruction_id": rule, "old_text": "before showing money", "text": "before displaying money"})
    assert res.get("success") and res["requires_approval"], res
    live = world.mcp(query_user, "read_instruction", {"instruction_id": rule, "report_id": reports["query_only"]["disputes"]})
    assert live["instruction"]["text"] == text and live["instruction"]["pending_review"]
    ctx = world.mcp(query_user, "get_context", {"report_id": reports["query_only"]["disputes"]})
    assert [i.get("text") for i in ctx["instructions"] if i["id"] == rule] == [text]

    # ── a text edit by a manager of every target is live and keeps every placement ──
    res = world.mcp(manager_all, "edit_instruction", {
        "instruction_id": rule, "old_text": "divide by 100", "text": "divide by 100 (never round)"})
    assert res.get("success") and not res["requires_approval"], res
    after = _detail(world, manager_all, rule)
    assert "never round" in after["text"]
    assert after["scope"] == "targeted"
    assert {f["id"] for f in after["folders"]} == {disputes, authorization}
    assert [a["id"] for a in after["agents"]] == [refunds]
    _assert_applies(world, personas, reports, rule, {"disputes", "authorization", "refunds"})

    # ── detaching one folder keeps the rule everywhere else ──
    res = world.mcp(manager_all, "edit_instruction", {"instruction_id": rule, "remove_folder_ids": [authorization]})
    assert res.get("success"), res
    _assert_applies(world, personas, reports, rule, {"disputes", "refunds"})

    # ── detaching the last targets is refused: never silently global, never orphaned ──
    res = world.mcp(manager_all, "edit_instruction", {
        "instruction_id": rule, "remove_folder_ids": [disputes], "remove_data_source_ids": [refunds]})
    assert not res.get("success") and res["rejected_reason"] == "would_remove_last_target", res
    res = world.mcp(world.admin, "edit_instruction", {"instruction_id": rule, "data_source_ids": [], "folder_ids": []})
    assert not res.get("success") and res["rejected_reason"] == "would_remove_last_target", res
    _assert_applies(world, personas, reports, rule, {"disputes", "refunds"})

    # ── making it global is explicit and needs org-level instruction admin ──
    res = world.mcp(manager_all, "edit_instruction", {"instruction_id": rule, "scope": "global"})
    assert not res.get("success") and res["rejected_reason"] == "permission_denied", res
    res = world.mcp(world.admin, "edit_instruction", {"instruction_id": rule, "scope": "global"})
    assert res.get("success") and res["instruction"]["scope"] == "global", res
    _assert_applies(world, personas, reports, rule, {"disputes", "authorization", "refunds", "other"})


@pytest.mark.e2e
def test_create_instruction_needs_an_explicit_destination(world):
    folder = world.folder("Ops")
    curator = world.user()
    world.grant_role(curator, [{"resource_type": "folder", "resource_id": folder, "permissions": ["manage_instructions"]}])

    for actor in (curator, world.admin):
        res = world.mcp(actor, "create_instruction", {"text": "Weeks start on Monday for every ops metric."})
        assert not res.get("success") and res["rejected_reason"] == "no_target", res
    res = world.mcp(curator, "create_instruction", {"text": "Weeks start on Monday for every ops metric.",
                                                    "scope": "global"})
    assert not res.get("success") and res["rejected_reason"] == "permission_denied", res
    res = world.mcp(world.admin, "create_instruction", {"text": "Weeks start on Monday for every ops metric.",
                                                        "scope": "global"})
    assert res.get("success") and res["scope"] == "global", res
    assert _detail(world, world.admin, res["instruction_id"])["scope"] == "global"

    # The single folder_id input still works, for a curator too (pending review:
    # curating is not managing).
    res = world.mcp(curator, "create_instruction", {"text": "Ops tickets close at 17:00 local.", "folder_id": folder})
    assert res.get("success") and res["folder_ids"] == [folder] and res["requires_approval"], res


@pytest.mark.e2e
def test_list_instructions_says_where_each_rule_applies(world):
    a, b = world.folder("North"), world.folder("South")
    tag = uuid.uuid4().hex[:6]
    shared = world.mcp(world.admin, "create_instruction", {"text": f"{tag} shared rule text", "folder_ids": [a, b]})
    only_a = world.mcp(world.admin, "create_instruction", {"text": f"{tag} north rule text", "folder_ids": [a]})
    glob = world.mcp(world.admin, "create_instruction", {"text": f"{tag} global rule text", "scope": "global"})
    assert all(x.get("success") for x in (shared, only_a, glob))

    out = world.mcp(world.admin, "list_instructions", {"search": tag, "limit": 50})
    rows = {i["id"]: i for i in out["instructions"]}
    assert rows[glob["instruction_id"]]["scope"] == "global"
    assert {f["id"] for f in rows[shared["instruction_id"]]["folders"]} == {a, b}
    assert rows[only_a["instruction_id"]]["scope"] == "targeted"

    in_b = world.mcp(world.admin, "list_instructions", {"search": tag, "folder_id": b})
    assert {i["id"] for i in in_b["instructions"]} == {shared["instruction_id"]} and in_b["total"] == 1


@pytest.mark.e2e
def test_shared_rule_hides_targets_the_reader_cannot_see(world):
    open_folder, private_folder = world.folder("Shared"), world.folder("Board")
    world.agent("open agent", open_folder)
    reader = world.user()
    world.share(open_folder, reader, "Viewer")
    rule = world.mcp(world.admin, "create_instruction", {
        "text": "Quarter boundaries follow the fiscal calendar.", "folder_ids": [open_folder, private_folder]})
    assert rule.get("success"), rule
    rid = _report(world, reader, folders=[open_folder])
    seen = world.mcp(reader, "read_instruction", {"instruction_id": rule["instruction_id"], "report_id": rid})
    assert seen.get("success"), seen
    assert [f["id"] for f in seen["instruction"]["folders"]] == [open_folder]
    assert seen["instruction"]["hidden_target_count"] == 1
    assert "Board" not in json.dumps(seen)


@pytest.mark.e2e
def test_browse_and_scope_a_report_by_folder(world):
    payments = world.folder("Payments")
    disputes = world.folder("Disputes", parent=payments)
    ledger = world.agent("dispute ledger", disputes)
    sibling = world.agent("dispute notes", disputes)
    elsewhere = world.agent("hr roster", world.folder("People"))
    analyst = world.user()
    world.share(payments, analyst, "Viewer")

    found = world.mcp(analyst, "browse_agents", {"search": "disputs"})  # a typo, as people type
    assert disputes in {f["id"] for f in found["folders"]}, found
    entry = next(f for f in found["folders"] if f["id"] == disputes)
    assert entry["path"].split(" / ")[0].startswith("Payments") and entry["can_query"]
    assert elsewhere not in {a["id"] for a in world.mcp(analyst, "browse_agents", {})["agents"]}
    # Two things named in one breath ("the ledger + notes agents") find both.
    both = world.mcp(analyst, "browse_agents", {"search": "ledger notes"})
    assert {ledger, sibling} <= {a["id"] for a in both["agents"]}, both

    made = world.mcp(analyst, "create_report", {"title": "Dispute aging", "agent_folder_ids": [disputes]})
    assert "_error" not in made, made
    assert made["scope"]["folders"][0]["id"] == disputes
    assert {d["id"] for d in made["data_sources"]} == {ledger, sibling}
    ctx = world.mcp(analyst, "get_context", {"report_id": made["report_id"]})
    assert ctx["scope"]["mode"] == "folders" and [f["id"] for f in ctx["scope"]["folders"]] == [disputes]
    assert set(ctx["scope"]["effective_agent_ids"]) == {ledger, sibling}

    # Mixed: a folder plus one agent from outside it.
    world.api(world.admin, "POST", f"data_sources/{elsewhere}/members",
              {"principal_type": "user", "principal_id": analyst["user_id"]})
    mixed = world.mcp(analyst, "create_report", {"title": "Mixed", "agent_folder_ids": [disputes],
                                                 "data_source_ids": [elsewhere]})
    assert mixed["scope"]["mode"] == "folders_and_agents", mixed
    assert {d["id"] for d in mixed["data_sources"]} == {ledger, sibling, elsewhere}

    # Title-only keeps working for existing clients: every agent the user can use.
    plain = world.mcp(analyst, "create_report", {"title": "Anything"})
    assert {ledger, sibling, elsewhere} <= {d["id"] for d in plain["data_sources"]}

    # A folder the caller can't use is refused, not ignored.
    hidden = world.folder("Board")
    refused = world.mcp(analyst, "create_report", {"title": "Nope", "agent_folder_ids": [hidden]})
    assert "_error" in refused, refused


@pytest.mark.e2e
def test_direct_agent_grant_reveals_no_parent_or_siblings(world):
    private = world.folder("Executive")
    target = world.agent("exec kpis", private)
    sibling = world.agent("exec comp", private)
    guest = world.user()
    world.api(world.admin, "POST", f"data_sources/{target}/members",
              {"principal_type": "user", "principal_id": guest["user_id"]})

    out = world.mcp(guest, "browse_agents", {})
    agents = {a["id"]: a for a in out["agents"]}
    assert target in agents and sibling not in agents
    assert agents[target]["folder_id"] is None and agents[target]["folder_path"] is None
    assert private not in {f["id"] for f in out["folders"]}
    assert "Executive" not in json.dumps(out)

    rid = _report(world, guest, agents=[target])
    ctx = world.mcp(guest, "get_context", {"report_id": rid})
    assert "Executive" not in json.dumps(ctx)
    refused = world.mcp(guest, "create_report", {"title": "x", "agent_folder_ids": [private]})
    assert "_error" in refused


@pytest.mark.e2e
def test_revoked_folder_never_widens_a_report_to_every_agent(world):
    disputes = world.folder("Disputes")
    world.agent("dispute ledger", disputes)
    public_agent = world._ds(f"company wiki {uuid.uuid4().hex[:4]}")
    world.api(world.admin, "PUT", f"data_sources/{public_agent['id']}", {"is_public": True})
    analyst = world.user()
    world.share(disputes, analyst, "Viewer")
    rid = _report(world, analyst, folders=[disputes])
    assert world.mcp(analyst, "get_context", {"report_id": rid}).get("report_id") == rid

    world.share(disputes, analyst, None)
    ctx = world.mcp(analyst, "get_context", {"report_id": rid})
    assert "_error" in ctx or public_agent["id"] not in {d["id"] for d in ctx.get("data_sources", [])}, ctx
    assert "_error" in ctx, "a report whose folder was revoked must not resolve to other agents"


@pytest.mark.e2e
def test_reports_stay_readable_after_a_folder_rule_is_turned_off_or_deleted(world):
    folder = world.folder("Collections")
    world.agent("collections ledger", folder)
    analyst = world.user()
    world.share(folder, analyst, "Viewer")
    rules = [world.mcp(world.admin, "create_instruction", {"text": f"Collections rule {i}: age from due date.",
                                                           "folder_ids": [folder]})["instruction_id"]
             for i in range(2)]
    rid = _report(world, analyst, folders=[folder])
    assert set(rules) <= _listed(world, analyst, rid)  # the report has now used both

    assert world.mcp(world.admin, "edit_instruction", {"instruction_id": rules[0], "status": "inactive"})["success"]
    assert world.mcp(world.admin, "delete_instruction", {"instruction_id": rules[1]})["success"]
    assert not set(rules) & _listed(world, analyst, rid)


@pytest.mark.e2e
@pytest.mark.parametrize("change", [
    {"old_text": " (or any other currency)", "text": ""},                       # a pure cut
    {"old_text": "in dollars (or any other currency)", "text": "in dollars"},    # a cut via replacement
    {"old_text": "Refund totals", "text": "Refund and chargeback totals"},        # an insertion
])
def test_curator_change_is_reported_as_waiting_for_review(world, change):
    folder = world.folder("Refund ops")
    curator = world.user()
    world.grant_role(curator, [{"resource_type": "folder", "resource_id": folder,
                                "permissions": ["view", "manage_instructions"]}])
    text = "Refund totals are shown in dollars (or any other currency) after dividing cents by 100."
    rule = world.mcp(world.admin, "create_instruction", {"text": text, "folder_ids": [folder]})["instruction_id"]

    res = world.mcp(curator, "edit_instruction", {"instruction_id": rule, **change})
    assert res.get("success"), res
    assert res["requires_approval"] and res["build_status"] == "pending_approval", res
    assert res["instruction"]["pending_review"]
    assert _detail(world, world.admin, rule)["pending_review"]


@pytest.mark.e2e
def test_a_pending_curator_change_does_not_block_other_authors(world):
    """Promoting a build re-syncs rules it does not change; a curator's pending
    edit on a rule shared with a folder the next author doesn't manage must not
    make that author's own, unrelated rule fail."""
    mine, theirs = world.folder("Disputes"), world.folder("Authorization")
    curator, manager = world.user(), world.user()
    world.grant_role(curator, [{"resource_type": "folder", "resource_id": f, "permissions": ["view", "manage_instructions"]}
                               for f in (mine, theirs)])
    world.share(mine, manager, "Manager")
    shared = world.mcp(world.admin, "create_instruction", {"text": "SLA clocks pause on public holidays.",
                                                            "folder_ids": [mine, theirs]})["instruction_id"]
    pending = world.mcp(curator, "edit_instruction", {"instruction_id": shared, "old_text": "public holidays",
                                                      "text": "public and bank holidays"})
    assert pending.get("success") and pending["requires_approval"], pending

    made = world.mcp(manager, "create_instruction", {"text": "Weekends don't count toward dispute SLAs.",
                                                     "folder_ids": [mine]})
    assert made.get("success") and not made["requires_approval"], made
    # ...and the curator's suggestion is still waiting, untouched.
    assert _detail(world, world.admin, shared)["pending_review"]
