import pytest  # type: ignore


def _build_headers(user_token: str, org_id: str):
    if user_token is None:
        pytest.fail("User token is required for git repository requests")
    if org_id is None:
        pytest.fail("Organization ID is required for git repository requests")
    return {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }


@pytest.fixture
def test_git_repository_connection(test_client):
    def _test_git_repository_connection(
        *,
        data_source_id: str,
        payload: dict,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = _build_headers(user_token, org_id)
        response = test_client.post(
            f"/api/data_sources/{data_source_id}/git_repository/test",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _test_git_repository_connection


@pytest.fixture
def create_git_repository(test_client):
    def _create_git_repository(
        *,
        data_source_id: str,
        payload: dict,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = _build_headers(user_token, org_id)
        response = test_client.post(
            f"/api/data_sources/{data_source_id}/git_repository",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _create_git_repository


@pytest.fixture
def get_git_repository(test_client):
    def _get_git_repository(
        *,
        data_source_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = _build_headers(user_token, org_id)
        response = test_client.get(
            f"/api/data_sources/{data_source_id}/git_repository",
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _get_git_repository


@pytest.fixture
def update_git_repository(test_client):
    def _update_git_repository(
        *,
        data_source_id: str,
        repository_id: str,
        payload: dict,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = _build_headers(user_token, org_id)
        response = test_client.put(
            f"/api/data_sources/{data_source_id}/git_repository/{repository_id}",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _update_git_repository


@pytest.fixture
def delete_git_repository(test_client):
    def _delete_git_repository(
        *,
        data_source_id: str,
        repository_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = _build_headers(user_token, org_id)
        response = test_client.delete(
            f"/api/data_sources/{data_source_id}/git_repository/{repository_id}",
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _delete_git_repository


@pytest.fixture
def index_git_repository(test_client):
    def _index_git_repository(
        *,
        data_source_id: str,
        repository_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = _build_headers(user_token, org_id)
        response = test_client.post(
            f"/api/data_sources/{data_source_id}/git_repository/{repository_id}/index",
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _index_git_repository


@pytest.fixture
def get_linked_instructions_count(test_client):
    """Get count of instructions linked to a git repository"""
    def _get_linked_instructions_count(
        *,
        data_source_id: str,
        repository_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = _build_headers(user_token, org_id)
        response = test_client.get(
            f"/api/data_sources/{data_source_id}/git_repository/{repository_id}/linked_instructions_count",
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _get_linked_instructions_count

