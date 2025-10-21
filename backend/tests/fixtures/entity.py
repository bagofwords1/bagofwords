import pytest


@pytest.fixture
def get_entities(test_client):
    def _get_entities(user_token=None, org_id=None, q=None, type=None, owner_id=None, data_source_id=None, skip=None, limit=None):
        if user_token is None:
            pytest.fail("User token is required for get_entities")
        if org_id is None:
            pytest.fail("Organization ID is required for get_entities")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        params = {}
        if q is not None:
            params["q"] = q
        if type is not None:
            params["type"] = type
        if owner_id is not None:
            params["owner_id"] = owner_id
        if data_source_id is not None:
            params["data_source_id"] = data_source_id
        if skip is not None:
            params["skip"] = skip
        if limit is not None:
            params["limit"] = limit

        response = test_client.get("/api/entities", headers=headers, params=params)
        assert response.status_code == 200, response.json()
        return response.json()

    return _get_entities


@pytest.fixture
def get_entity(test_client):
    def _get_entity(entity_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_entity")
        if org_id is None:
            pytest.fail("Organization ID is required for get_entity")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(f"/api/entities/{entity_id}", headers=headers)
        return response

    return _get_entity


@pytest.fixture
def create_global_entity(test_client):
    def _create_global_entity(
        *,
        title="Test Entity",
        slug="test-entity",
        status="draft",
        type="model",
        code="select 1 as value",
        description=None,
        data_source_ids=None,
        user_token=None,
        org_id=None,
    ):
        if user_token is None:
            pytest.fail("User token is required for create_global_entity")
        if org_id is None:
            pytest.fail("Organization ID is required for create_global_entity")

        payload = {
            "type": type,
            "title": title,
            "slug": slug,
            "description": description,
            "tags": [],
            "code": code,
            "data": {},
            "status": status,
            "data_source_ids": data_source_ids or [],
        }

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            "/api/entities/global",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _create_global_entity

