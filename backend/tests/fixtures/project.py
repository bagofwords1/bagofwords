import pytest


def _headers(user_token, org_id):
    return {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }


@pytest.fixture
def create_project(test_client):
    def _create_project(name="Test Project", user_token=None, org_id=None,
                        description=None, color=None, expect_status=200):
        if user_token is None or org_id is None:
            pytest.fail("user_token and org_id are required for create_project")
        response = test_client.post(
            "/api/projects",
            json={"name": name, "description": description, "color": color},
            headers=_headers(user_token, org_id),
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response.json() if response.status_code == 200 else response

    return _create_project


@pytest.fixture
def list_projects(test_client):
    def _list_projects(user_token=None, org_id=None, expect_status=200):
        if user_token is None or org_id is None:
            pytest.fail("user_token and org_id are required for list_projects")
        response = test_client.get("/api/projects", headers=_headers(user_token, org_id))
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response.json()

    return _list_projects


@pytest.fixture
def get_project(test_client):
    def _get_project(project_id, user_token=None, org_id=None, expect_status=200):
        if user_token is None or org_id is None:
            pytest.fail("user_token and org_id are required for get_project")
        response = test_client.get(
            f"/api/projects/{project_id}", headers=_headers(user_token, org_id)
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response

    return _get_project


@pytest.fixture
def update_project(test_client):
    def _update_project(project_id, user_token=None, org_id=None, expect_status=200, **fields):
        if user_token is None or org_id is None:
            pytest.fail("user_token and org_id are required for update_project")
        response = test_client.put(
            f"/api/projects/{project_id}", json=fields, headers=_headers(user_token, org_id)
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response

    return _update_project


@pytest.fixture
def delete_project(test_client):
    def _delete_project(project_id, user_token=None, org_id=None, expect_status=200):
        if user_token is None or org_id is None:
            pytest.fail("user_token and org_id are required for delete_project")
        response = test_client.delete(
            f"/api/projects/{project_id}", headers=_headers(user_token, org_id)
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response

    return _delete_project


@pytest.fixture
def upsert_project_member(test_client):
    def _upsert(project_id, member_user_id, permissions=None,
                user_token=None, org_id=None, expect_status=200):
        if user_token is None or org_id is None:
            pytest.fail("user_token and org_id are required for upsert_project_member")
        response = test_client.put(
            f"/api/projects/{project_id}/members",
            json={"user_id": member_user_id, "permissions": permissions or ["view"]},
            headers=_headers(user_token, org_id),
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response

    return _upsert


@pytest.fixture
def remove_project_member(test_client):
    def _remove(project_id, member_user_id, user_token=None, org_id=None, expect_status=200):
        if user_token is None or org_id is None:
            pytest.fail("user_token and org_id are required for remove_project_member")
        response = test_client.delete(
            f"/api/projects/{project_id}/members/{member_user_id}",
            headers=_headers(user_token, org_id),
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response

    return _remove


@pytest.fixture
def move_report_to_project(test_client):
    """Move via the report update endpoint (sentinel project_id field)."""
    def _move(report_id, project_id, user_token=None, org_id=None, expect_status=200):
        if user_token is None or org_id is None:
            pytest.fail("user_token and org_id are required for move_report_to_project")
        response = test_client.put(
            f"/api/reports/{report_id}",
            json={"project_id": project_id if project_id is not None else ""},
            headers=_headers(user_token, org_id),
        )
        if expect_status:
            assert response.status_code == expect_status, response.json()
        return response

    return _move
