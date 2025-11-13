import pytest


@pytest.fixture
def get_test_suites(test_client):
    def _get_test_suites(*, user_token=None, org_id=None, page=None, limit=None, search=None):
        if user_token is None:
            pytest.fail("User token is required for get_test_suites")
        if org_id is None:
            pytest.fail("Organization ID is required for get_test_suites")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        params = {}
        if page is not None:
            params["page"] = page
        if limit is not None:
            params["limit"] = limit
        if search is not None:
            params["search"] = search

        response = test_client.get("/api/tests/suites", headers=headers, params=params)
        assert response.status_code == 200, response.json()
        return response.json()

    return _get_test_suites


@pytest.fixture
def get_test_suite(test_client):
    def _get_test_suite(suite_id, *, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_test_suite")
        if org_id is None:
            pytest.fail("Organization ID is required for get_test_suite")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(f"/api/tests/suites/{suite_id}", headers=headers)
        return response

    return _get_test_suite


@pytest.fixture
def create_test_suite(test_client):
    def _create_test_suite(*, name="Sample Suite", description=None, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for create_test_suite")
        if org_id is None:
            pytest.fail("Organization ID is required for create_test_suite")

        payload = {
            "name": name,
            "description": description,
        }

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            "/api/tests/suites",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _create_test_suite


@pytest.fixture
def get_test_cases(test_client):
    def _get_test_cases(suite_id, *, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_test_cases")
        if org_id is None:
            pytest.fail("Organization ID is required for get_test_cases")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(f"/api/tests/suites/{suite_id}/cases", headers=headers)
        assert response.status_code == 200, response.json()
        return response.json()

    return _get_test_cases


@pytest.fixture
def get_test_case(test_client):
    def _get_test_case(case_id, *, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_test_case")
        if org_id is None:
            pytest.fail("Organization ID is required for get_test_case")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(f"/api/tests/cases/{case_id}", headers=headers)
        return response

    return _get_test_case


@pytest.fixture
def create_test_case(test_client):
    def _create_test_case(
        *,
        suite_id,
        name="Sample Case",
        prompt_json=None,
        expectations_json=None,
        data_source_ids_json=None,
        user_token=None,
        org_id=None,
    ):
        if user_token is None:
            pytest.fail("User token is required for create_test_case")
        if org_id is None:
            pytest.fail("Organization ID is required for create_test_case")
        if not suite_id:
            pytest.fail("suite_id is required for create_test_case")

        # Minimal valid shapes for service/schema:
        prompt = prompt_json or {
            "content": "Evaluate this output.",
        }
        expectations = expectations_json or {
            "spec_version": 1,
            "rules": [],
            "order_mode": "flexible",
        }

        payload = {
            "name": name,
            "prompt_json": prompt,
            "expectations_json": expectations,
            "data_source_ids_json": data_source_ids_json or [],
        }

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            f"/api/tests/suites/{suite_id}/cases",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _create_test_case


