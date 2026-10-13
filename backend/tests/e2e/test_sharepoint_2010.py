"""Connection lifecycle with the real app/DB and a simulated legacy HTTP server."""
import uuid
import pytest
from tests.unit.test_sharepoint_2010_client import legacy, SITE, ROOT

def wait_for_index(test_client, connection_id, headers):
    # Refresh is asynchronous. Observe the public job state before reading tables
    # or changing scope, instead of relying on SQLite's incidental timing.
    for _ in range(500):
        response = test_client.get(f'/api/connections/{connection_id}/indexing', headers=headers)
        assert response.status_code == 200, response.text
        state = response.json()
        if state['status'] in ('completed', 'failed', 'cancelled'):
            assert state['status'] == 'completed', state
            return
    pytest.fail('Indexing did not finish within the bounded status poll.')


@pytest.mark.e2e
def test_legacy_connection_roundtrip_catalog_and_member_boundary(
    legacy, test_client, create_user, login_user, whoami, create_connection,
    get_connection, test_connection_connectivity, refresh_connection_schema, get_connection_tables,
):
    admin = create_user()
    token = login_user(admin['email'], admin['password'])
    org = whoami(token)['organizations'][0]['id']
    headers = {'Authorization': f'Bearer {token}', 'X-Organization-Id': org}
    config = {'site_url': SITE, 'api_mode': '2010', 'drive_name': 'Documents', 'recursive': True}
    payload = {'name': 'Legacy documents', 'type': 'sharepoint_onprem', 'config': config,
               'credentials': {'username': 'DOMAIN\\reader', 'password': 'synthetic-private-secret'}}
    probe = test_client.post('/api/connections/test-params', json=payload, headers=headers)
    assert probe.status_code == 200 and probe.json()['success'], probe.json()
    conn = create_connection(**payload, user_token=token, org_id=org)
    saved = get_connection(connection_id=conn['id'], user_token=token, org_id=org)
    assert saved['config']['api_mode'] == '2010'
    assert 'synthetic-private-secret' not in str(saved)
    assert test_connection_connectivity(connection_id=conn['id'], user_token=token, org_id=org)['success']
    refresh_connection_schema(connection_id=conn['id'], user_token=token, org_id=org)
    wait_for_index(test_client, conn['id'], headers)
    tables = get_connection_tables(connection_id=conn['id'], user_token=token, org_id=org)
    assert len(tables) == sum(p.startswith(ROOT + '/') for p in legacy.blobs)
    changed = test_client.put(f"/api/connections/{conn['id']}", json={'config': {**config, 'folder_path': 'nested'}}, headers=headers)
    assert changed.status_code == 200, changed.text
    assert get_connection(connection_id=conn['id'], user_token=token, org_id=org)['config']['api_mode'] == '2010'
    refresh_connection_schema(connection_id=conn['id'], user_token=token, org_id=org)
    wait_for_index(test_client, conn['id'], headers)
    assert len(get_connection_tables(connection_id=conn['id'], user_token=token, org_id=org)) == 1
    email = f'legacy-member-{uuid.uuid4().hex[:8]}@example.com'
    invite = test_client.post(f'/api/organizations/{org}/members', json={'organization_id': org, 'email': email, 'role': 'member'}, headers=headers)
    assert invite.status_code == 200
    member = create_user(email=email)
    mt = login_user(member['email'], member['password'])
    denied = test_client.put(f"/api/connections/{conn['id']}", json={'config': {'api_mode': 'modern'}}, headers={**headers, 'Authorization': f'Bearer {mt}'})
    assert denied.status_code == 403

@pytest.mark.e2e
@pytest.mark.parametrize('config', [{'api_mode': '2010', 'drive_name': ''}, {'api_mode': 'unsupported'}])
def test_invalid_legacy_configuration_cannot_connect(legacy, test_client, create_user, login_user, whoami, config):
    user = create_user()
    token = login_user(user['email'], user['password'])
    org = whoami(token)['organizations'][0]['id']
    response = test_client.post('/api/connections/test-params', json={'name': 'Invalid', 'type': 'sharepoint_onprem',
        'config': {'site_url': SITE, **config}, 'credentials': {'username': 'DOMAIN\\reader', 'password': 'synthetic'}},
        headers={'Authorization': f'Bearer {token}', 'X-Organization-Id': org})
    assert response.status_code in (400, 422) or (response.status_code == 200 and not response.json()['success'])
    assert not legacy.calls
