"""A folder's instruction list carries the row fields an agent's list does
(status, load mode, category, source, agents, manage authority), and never
names an agent the viewer cannot see."""


def test_folder_instruction_rows_match_agent_rows(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source):
    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    member = invite_user_to_org(org_id=org, admin_token=admin["token"])

    def request(actor, method, path, **data):
        return test_client.request(
            method,
            "/api/" + path,
            json=data,
            headers={"Authorization": "Bearer " + actor["token"], "X-Organization-Id": org},
        )

    folder = request(admin, "POST", "folders", name="Logistics").json()["id"]
    hidden = sqlite_data_source(name="Secret payroll", user_token=admin["token"], org_id=org)["id"]
    shared = sqlite_data_source(name="Purchasing", user_token=admin["token"], org_id=org, is_public=True)["id"]

    plain = request(
        admin, "POST", f"folders/{folder}/knowledge/instructions",
        title="Source selection", text="Prefer ERP tables.", load_mode="intelligent",
    )
    assert plain.is_success, plain.text
    linked = request(
        admin, "POST", "instructions", title="Shared routing", text="Route POs to Purchasing.",
        status="published", load_mode="always", category="general",
        folder_ids=[folder], data_source_ids=[hidden, shared],
    )
    assert linked.is_success, linked.text

    rows = {r["id"]: r for r in request(admin, "GET", f"folders/{folder}/knowledge/instructions").json()}
    row = rows[plain.json()["id"]]
    assert row["title"] == "Source selection"
    assert (row["status"], row["load_mode"], row["source_type"]) == ("published", "intelligent", "user")
    assert row["can_manage_scope"] is True
    assert "text" not in row  # the tree list stays body-free; details=true adds it
    assert {d["name"] for d in rows[linked.json()["id"]]["data_sources"]} == {"Secret payroll", "Purchasing"}
    detailed = request(admin, "GET", f"folders/{folder}/knowledge/instructions?details=true").json()
    assert {r["text"] for r in detailed} == {"Prefer ERP tables.", "Route POs to Purchasing."}

    role = request(
        admin, "POST", f"organizations/{org}/roles", name="Logistics readers",
        resource_grants=[{"resource_type": "folder", "resource_id": folder, "permissions": ["view"]}],
    ).json()["id"]
    assert request(
        admin, "POST", f"organizations/{org}/role-assignments",
        role_id=role, principal_type="user", principal_id=member["user_id"],
    ).is_success
    viewed = request(member, "GET", f"folders/{folder}/knowledge/instructions")
    assert viewed.is_success, viewed.text
    assert viewed.json(), "the viewer should see the folder's published instructions"
    for r in viewed.json():
        assert r["can_manage_scope"] is False
        assert "Secret payroll" not in {d["name"] for d in r["data_sources"]}
