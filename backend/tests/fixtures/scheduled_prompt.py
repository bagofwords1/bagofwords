import pytest


@pytest.fixture
def create_scheduled_prompt(test_client):
    def _create_scheduled_prompt(
        report_id,
        prompt=None,
        cron_schedule="0 9 * * *",
        notification_subscribers=None,
        user_token=None,
        org_id=None,
        expect_status=200,
    ):
        if user_token is None:
            pytest.fail("User token is required for create_scheduled_prompt")
        if org_id is None:
            pytest.fail("Organization ID is required for create_scheduled_prompt")

        payload = {
            "prompt": prompt or {"content": "Test scheduled prompt"},
            "cron_schedule": cron_schedule,
        }
        if notification_subscribers is not None:
            payload["notification_subscribers"] = notification_subscribers

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            f"/api/reports/{report_id}/scheduled-prompts",
            json=payload,
            headers=headers,
        )
        assert response.status_code == expect_status, response.json()
        if expect_status == 200:
            return response.json()
        return response

    return _create_scheduled_prompt


@pytest.fixture
def list_scheduled_prompts(test_client):
    def _list_scheduled_prompts(report_id, user_token=None, org_id=None, expect_status=200):
        if user_token is None:
            pytest.fail("User token is required for list_scheduled_prompts")
        if org_id is None:
            pytest.fail("Organization ID is required for list_scheduled_prompts")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(
            f"/api/reports/{report_id}/scheduled-prompts",
            headers=headers,
        )
        assert response.status_code == expect_status, response.json()
        if expect_status == 200:
            return response.json()
        return response

    return _list_scheduled_prompts


@pytest.fixture
def update_scheduled_prompt(test_client):
    def _update_scheduled_prompt(
        report_id,
        sp_id,
        user_token=None,
        org_id=None,
        expect_status=200,
        **fields,
    ):
        if user_token is None:
            pytest.fail("User token is required for update_scheduled_prompt")
        if org_id is None:
            pytest.fail("Organization ID is required for update_scheduled_prompt")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.put(
            f"/api/reports/{report_id}/scheduled-prompts/{sp_id}",
            json=fields,
            headers=headers,
        )
        assert response.status_code == expect_status, response.json() if response.status_code != 204 else ""
        if expect_status == 200:
            return response.json()
        return response

    return _update_scheduled_prompt


@pytest.fixture
def delete_scheduled_prompt(test_client):
    def _delete_scheduled_prompt(report_id, sp_id, user_token=None, org_id=None, expect_status=204):
        if user_token is None:
            pytest.fail("User token is required for delete_scheduled_prompt")
        if org_id is None:
            pytest.fail("Organization ID is required for delete_scheduled_prompt")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.delete(
            f"/api/reports/{report_id}/scheduled-prompts/{sp_id}",
            headers=headers,
        )
        assert response.status_code == expect_status
        return response

    return _delete_scheduled_prompt
