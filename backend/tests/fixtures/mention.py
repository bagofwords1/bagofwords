import pytest
from fastapi.testclient import TestClient
from main import app
import os


@pytest.fixture
def get_available_mentions(test_client):
    def _get_available_mentions(user_token=None, org_id=None, data_source_ids=None, categories=None):
        if user_token is None:
            pytest.fail("User token is required for get_available_mentions")
        if org_id is None:
            pytest.fail("Organization ID is required for get_available_mentions")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        params = {}
        if data_source_ids:
            if isinstance(data_source_ids, list):
                params["data_source_ids"] = ",".join(str(x) for x in data_source_ids)
            else:
                params["data_source_ids"] = str(data_source_ids)
        if categories:
            if isinstance(categories, list):
                params["categories"] = ",".join(categories)
            else:
                params["categories"] = str(categories)

        response = test_client.get(
            "/api/mentions/available",
            headers=headers,
            params=params
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _get_available_mentions


