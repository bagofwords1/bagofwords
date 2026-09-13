"""Management tests validate saved organization credentials, not query preferences."""
import pytest


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize('personal_only', [False, True])
@pytest.mark.parametrize('signed_in', [False, True])
async def test_management_test_uses_configured_account_scope(
    personal_only, signed_in, monkeypatch, test_client, bootstrap_admin, create_connection,
    invite_user_to_org,
):
    from app.data_sources.clients.powerbi_client import PowerBIClient

    # Stub only the external driver's probe. Credential resolution remains real.
    async def probe(self):
        return {'success': self.client_secret == 'valid-org-secret' or self._access_token == 'valid-personal-token', 'message': 'Probe result'}
    monkeypatch.setattr(PowerBIClient, 'atest_connection', probe)
    monkeypatch.setattr(PowerBIClient, 'get_schemas', lambda self, **kwargs: [])
    admin = bootstrap_admin('admin')
    headers = {'Authorization': f"Bearer {admin['token']}", 'X-Organization-Id': admin['org_id']}
    conn = create_connection(
        name='Management identity', type='powerbi',
        config={'auth_type': 'oauth' if personal_only else 'service_principal'},
        credentials={'tenant_id': 'tenant-test', 'client_id': 'client-test', 'client_secret': 'valid-org-secret'},
        auth_policy='user_required', allowed_user_auth_modes=['oauth'],
        user_token=admin['token'], org_id=admin['org_id'],
    )
    url = f"/api/connections/{conn['id']}"
    if signed_in:
        # Simulate the OAuth provider's completed callback. The credentials API
        # intentionally does not accept OAuth tokens directly from a client.
        from app.dependencies import async_session_maker
        from app.models.user_connection_credentials import UserConnectionCredentials
        async with async_session_maker() as db:
            row = UserConnectionCredentials(connection_id=conn['id'], user_id=admin['user_id'],
                organization_id=admin['org_id'], auth_mode='oauth', is_active=True, is_primary=True)
            row.encrypt_credentials({'access_token': 'valid-personal-token'})
            db.add(row)
            await db.commit()
    # The query preference is self in both cases, independently of management.
    result = test_client.post(url + '/test', headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['success'] is (not personal_only or signed_in)
    detail = test_client.get(url, headers=headers).json()
    assert detail['management_auth'] == ('user' if personal_only else 'system')
    if personal_only:
        assert detail['last_connection_status'] is None
        from app.dependencies import async_session_maker
        from app.models.connection import Connection
        async with async_session_maker() as db:
            stored = await db.get(Connection, conn['id'])
            assert stored.last_connection_status is None
    else:
        assert detail['last_connection_status'] == 'success'
        # Draft invalid organization credentials must fail even though stored ones work.
        failed = test_client.post(url + '/test', headers=headers, json={'credentials': {'client_secret': 'invalid'}})
        assert failed.json()['success'] is False

    member = invite_user_to_org(org_id=admin['org_id'], admin_token=admin['token'])
    denied = test_client.post(url + '/test', headers={**headers, 'Authorization': f"Bearer {member['token']}"})
    assert denied.status_code == 403
