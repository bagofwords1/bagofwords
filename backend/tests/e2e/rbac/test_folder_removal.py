import pytest


@pytest.mark.e2e
def test_folder_removal_modes(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, monkeypatch):
    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    member = invite_user_to_org(org_id=org, admin_token=admin["token"])

    def request(method, path, actor=admin, **body):
        return test_client.request(
            method,
            "/api/" + path,
            json=body,
            headers={"Authorization": "Bearer " + actor["token"], "X-Organization-Id": org},
        )

    def create(path, **body):
        result = request("POST", path, **body)
        assert result.is_success, result.text
        return result.json()["id"]

    parent = create("folders", name="Portfolio")
    old = create("folders", name="Campaigns", parent_id=parent)
    child = create("folders", name="Brand", parent_id=old)
    peer = create("folders", name="Shared")
    agent = sqlite_data_source(name="Campaign agent", org_id=org, user_token=admin["token"])["id"]
    assert request("PUT", f"folders/items/agent/{agent}/placement", parent_id=old).is_success
    knowledge = f"folders/{old}/knowledge/"
    rule = create(knowledge + "instructions", title="Routing", text="Route campaigns")
    suite = create(knowledge + "evals", name="Checks")
    assert request("PUT", f"folders/{peer}/knowledge/instructions/{rule}").is_success
    assert request("DELETE", f"folders/{old}?mode=contents", member).status_code == 403
    kept = request("DELETE", f"folders/{old}?mode=keep")
    assert kept.status_code == 204, kept.text
    assert request("GET", f"data_sources/{agent}").json()["folder_id"] == parent
    assert request("GET", f"folders/{parent}/knowledge/evals").json()[0]["id"] == suite
    assert next(f for f in request("GET", "folders").json()["folders"] if f["id"] == child)["parent_id"] == parent

    assert request(
        "PUT", f"folders/{parent}/share", principal_type="user", principal_id=member["user_id"], role="Manager"
    ).is_success
    assert request(
        "PUT", f"folders/{child}/share", principal_type="user", principal_id=admin["user_id"], role="Manager"
    ).is_success
    assert request("PUT", f"folders/{child}/inheritance", limited_access=True).is_success
    denied = request("DELETE", f"folders/{parent}?mode=contents", member)
    assert denied.status_code == 403, denied.text
    assert request("GET", f"data_sources/{agent}").is_success
    exclusive = create(f"folders/{child}/knowledge/instructions", title="Private", text="Private rule")
    headers = {"Authorization": "Bearer " + admin["token"], "X-Organization-Id": org}
    upload = test_client.post(
        f"/api/folders/{child}/knowledge/files",
        headers=headers,
        files={"file": ("example.txt", b"private file", "text/plain")},
    )
    assert upload.is_success, upload.text
    file_id = upload.json()["id"]
    from fastapi import HTTPException
    from app.services.data_source_service import DataSourceService

    original_delete = DataSourceService.delete_data_source

    async def fail_after_agent(self, *args):
        await original_delete(self, *args)
        raise HTTPException(409, "Injected failure after deleting the agent")

    with monkeypatch.context() as patch:
        patch.setattr(DataSourceService, "delete_data_source", fail_after_agent)
        assert request("DELETE", f"folders/{parent}?mode=contents").status_code == 409
    assert request("GET", f"folders/{child}/knowledge/files").json()[0]["id"] == file_id
    assert request("GET", f"files/{file_id}/content").is_success
    assert request("GET", f"folders/{child}/knowledge/instructions").json()[0]["id"] == exclusive
    assert request("GET", f"data_sources/{agent}").is_success
    result = request("DELETE", f"folders/{parent}?mode=contents")
    assert result.status_code == 204, result.text
    assert request("GET", f"data_sources/{agent}").status_code == 404
    assert request("GET", f"tests/suites/{suite}").status_code == 404
    assert request("GET", f"instructions/{exclusive}").status_code == 404
    assert request("GET", f"folders/{peer}/knowledge/instructions").json()[0]["id"] == rule
