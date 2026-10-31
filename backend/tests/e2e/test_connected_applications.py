"""Capabilities share application identity without changing OAuth contracts."""
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

from tests.e2e.test_oauth_app import APP_REDIRECT_URI, _issue_token, _pkce_pair, _allow_multiple_orgs


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_capabilities_share_identity_and_lifecycle_revokes_tokens(test_client, create_user, login_user, whoami, action):
    user = create_user()
    token = login_user(user['email'], user['password'])
    org = whoami(token)['organizations'][0]['id']
    headers = {'Authorization': f'Bearer {token}', 'X-Organization-Id': org}
    created = test_client.post('/api/connected_applications', headers=headers, json={'name': 'Customer portal'})
    assert created.status_code == 201, created.text
    application = created.json()
    assert application['agent_ids'] == []
    oauth = test_client.post('/api/oauth/clients', headers=headers, json={
        'application_id': application['id'], 'name': 'Ignored duplicate name',
        'redirect_uris': [APP_REDIRECT_URI], 'scopes': 'app mcp', 'trusted': True,
    })
    assert oauth.status_code == 200, oauth.text
    client = oauth.json()
    assert client['id'] == application['id']
    assert client['name'] == application['name']
    host = test_client.post('/api/host_applications', headers=headers, json={
        'application_id': application['id'], 'name': 'Another name',
        'identity_mode': 'sso', 'allowed_origins': ['https://portal.example.com'],
    })
    assert host.status_code == 201, host.text
    assert host.json()['id'] == application['id']
    apps = test_client.get('/api/connected_applications', headers=headers).json()
    assert len(apps) == 1
    assert apps[0]['oauth'] and apps[0]['embedding']
    renamed = test_client.patch('/api/connected_applications/' + application['id'], headers=headers, json={'name': 'Renamed portal'})
    assert renamed.status_code == 200, renamed.text
    oauth_after = test_client.get('/api/oauth/clients', headers=headers).json()[0]
    host_after = test_client.get('/api/host_applications', headers=headers).json()[0]
    assert oauth_after['name'] == host_after['name'] == 'Renamed portal'
    assert oauth_after['client_id'] == client['client_id']
    assert host_after['public_id'] == host.json()['public_id']
    tokens = _issue_token(test_client, login_token=token, client=client, scope='app')
    verifier, challenge = _pkce_pair()
    pending = test_client.post('/api/oauth/authorize', headers=headers, json={
        'client_id': client['client_id'], 'redirect_uri': APP_REDIRECT_URI, 'scope': 'app',
        'state': 'pending-grant', 'code_challenge': challenge, 'code_challenge_method': 'S256',
    })
    assert pending.status_code == 200, pending.text
    code = parse_qs(urlparse(pending.json()['redirect_url']).query)['code'][0]
    access_headers = {'Authorization': f"Bearer {tokens['access_token']}", 'X-Organization-Id': org}
    assert test_client.get('/api/reports', headers=access_headers).status_code == 200
    path = '/api/connected_applications/' + application['id']
    response = (test_client.patch(path, headers=headers, json={'enabled': False}) if action == 'disable'
                else test_client.delete(path, headers=headers))
    assert response.status_code == (200 if action == 'disable' else 204), response.text
    assert test_client.get('/api/reports', headers=access_headers).status_code == 401
    refresh = test_client.post('/api/oauth/token', data={'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'], 'client_id': client['client_id']})
    assert refresh.status_code == 400
    if action == 'disable':
        assert test_client.patch(path, headers=headers, json={'enabled': True}).status_code == 200
    else:
        assert test_client.get('/api/connected_applications', headers=headers).json() == []
    assert test_client.get('/api/reports', headers=access_headers).status_code == 401
    exchanged = test_client.post('/api/oauth/token', data={
        'grant_type': 'authorization_code', 'code': code, 'code_verifier': verifier,
        'client_id': client['client_id'], 'client_secret': client['client_secret'], 'redirect_uri': APP_REDIRECT_URI,
    })
    assert exchanged.status_code == 400, exchanged.text


def test_members_cannot_register_connected_applications(test_client, create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin['email'], admin['password'])
    org = whoami(token)['organizations'][0]['id']
    email = f'connected-member-{secrets.token_hex(6)}@example.com'
    headers = {'Authorization': f'Bearer {token}', 'X-Organization-Id': org}
    assert test_client.post(f'/api/organizations/{org}/members', headers=headers, json={'organization_id': org, 'email': email, 'role': 'member'}).status_code == 200
    member = create_user(email=email)
    member_token = login_user(member['email'], member['password'])
    member_headers = {'Authorization': f'Bearer {member_token}', 'X-Organization-Id': org}
    for method in ('get', 'post'):
        kwargs = {'json': {'name': 'Forbidden'}} if method == 'post' else {}
        assert getattr(test_client, method)('/api/connected_applications', headers=member_headers, **kwargs).status_code == 403


def test_migration_preserves_oauth_credentials_and_pending_grants(test_client, create_user, login_user, whoami, create_oauth_client):
    """The forward migration preserves real OAuth credentials and pending grants."""
    import asyncio
    import importlib.util
    from pathlib import Path
    from urllib.parse import parse_qs, urlparse
    from sqlalchemy import text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.dependencies import async_session_maker
    from tests.e2e.test_oauth_app import _pkce_pair

    user = create_user()
    token = login_user(user['email'], user['password'])
    org = whoami(token)['organizations'][0]['id']
    client = create_oauth_client(user_token=token, org_id=org, name='Existing integration',
                                 redirect_uris=[APP_REDIRECT_URI], scopes='app mcp', trusted=True)
    # Match the released schema's unrestricted application policy before
    # round-tripping API-created credentials through the legacy table.
    assert test_client.patch('/api/connected_applications/' + client['id'],
                             headers={'Authorization': f'Bearer {token}', 'X-Organization-Id': org},
                             json={'agent_ids': None}).status_code == 200
    tokens = _issue_token(test_client, login_token=token, client=client, scope='app')
    verifier, challenge = _pkce_pair()
    pending = test_client.post('/api/oauth/authorize', headers={'Authorization': f'Bearer {token}'}, json={
        'client_id': client['client_id'], 'redirect_uri': APP_REDIRECT_URI, 'scope': 'mcp',
        'state': 'preserved', 'code_challenge': challenge, 'code_challenge_method': 'S256',
    })
    assert pending.status_code == 200, pending.text
    code = parse_qs(urlparse(pending.json()['redirect_url']).query)['code'][0]

    async def migrate():
        path = Path(__file__).parents[2] / 'alembic/versions/connapps01_connected_applications.py'
        spec = importlib.util.spec_from_file_location('connapps01_preservation', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        async with async_session_maker() as db:
            def run(session):
                conn = session.connection()
                with Operations.context(MigrationContext.configure(conn)):
                    # The API cannot create a pre-migration schema. Downgrade only
                    # this isolated test DB, then migrate its real API-created grants.
                    migration.downgrade()
                    before = dict(conn.execute(text('SELECT * FROM oauth_mcp_clients WHERE id=:id'), {'id': client['id']}).mappings().one())
                    migration.upgrade()
                    after = dict(conn.execute(text('SELECT c.*, a.name, a.organization_id, a.created_at, a.updated_at, a.deleted_at FROM application_oauth_config c JOIN connected_applications a ON a.id=c.id WHERE c.id=:id'), {'id': client['id']}).mappings().one())
                    assert before == after
                    # Idempotence for the unstamped dashboard migration corpus.
                    migration.upgrade()
            await db.run_sync(run)
            await db.commit()
    asyncio.run(migrate())
    headers = {'Authorization': f"Bearer {tokens['access_token']}", 'X-Organization-Id': org}
    assert test_client.get('/api/reports', headers=headers).status_code == 200
    refreshed = test_client.post('/api/oauth/token', data={
        'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
        'client_id': client['client_id'], 'client_secret': client['client_secret'],
    })
    assert refreshed.status_code == 200, refreshed.text
    exchanged = test_client.post('/api/oauth/token', data={
        'grant_type': 'authorization_code', 'code': code, 'code_verifier': verifier,
        'client_id': client['client_id'], 'client_secret': client['client_secret'], 'redirect_uri': APP_REDIRECT_URI,
    })
    assert exchanged.status_code == 200, exchanged.text
    apps = test_client.get('/api/connected_applications', headers={'Authorization': f'Bearer {token}', 'X-Organization-Id': org}).json()
    assert apps[0]['agent_ids'] is None


def test_application_attachment_is_org_scoped_and_unique(test_client, create_user, login_user, whoami, create_organization, _allow_multiple_orgs):
    import uuid
    user = create_user()
    token = login_user(user['email'], user['password'])
    org = whoami(token)['organizations'][0]['id']
    headers = {'Authorization': f'Bearer {token}', 'X-Organization-Id': org}
    app = test_client.post('/api/connected_applications', headers=headers, json={'name': 'Portal'}).json()
    outside_org = create_organization(name='Other application tenant', user_token=token)
    outside_headers = {'Authorization': f'Bearer {token}', 'X-Organization-Id': outside_org}
    assert test_client.get('/api/connected_applications', headers=outside_headers).json() == []
    assert test_client.patch('/api/connected_applications/' + app['id'], headers=outside_headers, json={'enabled': False}).status_code == 404
    assert test_client.delete('/api/connected_applications/' + app['id'], headers=outside_headers).status_code == 404
    for path, body in [('/api/oauth/clients', {'name': 'OAuth', 'scopes': 'app', 'redirect_uris': [APP_REDIRECT_URI]}),
                       ('/api/host_applications', {'name': 'Embed', 'identity_mode': 'sso'})]:
        outside = test_client.post(path, headers=outside_headers, json={**body, 'application_id': app['id']})
        assert outside.status_code == 404, outside.text
        missing = test_client.post(path, headers=headers, json={**body, 'application_id': str(uuid.uuid4())})
        assert missing.status_code == 404
        created = test_client.post(path, headers=headers, json={**body, 'application_id': app['id']})
        assert created.status_code in (200, 201), created.text
        duplicate = test_client.post(path, headers=headers, json={**body, 'application_id': app['id']})
        assert duplicate.status_code == 409, duplicate.text
    invalid_agent = test_client.patch('/api/connected_applications/' + app['id'], headers=headers, json={'agent_ids': [str(uuid.uuid4())]})
    assert invalid_agent.status_code == 400


@pytest.mark.parametrize('role', ['admin', 'member'])
def test_agent_ceiling_limits_public_agents_and_cannot_grant_private_access(
    test_client, create_user, login_user, whoami, dynamic_sqlite_db, role, enable_mcp,
):
    admin = create_user()
    admin_token = login_user(admin['email'], admin['password'])
    org = whoami(admin_token)['organizations'][0]['id']
    headers = {'Authorization': f'Bearer {admin_token}', 'X-Organization-Id': org}
    agents = []
    for name, public in [('Allowed public', True), ('Excluded public', True), ('Private agent', False)]:
        result = test_client.post('/api/data_sources', headers=headers, json={
            'name': name, 'type': 'sqlite', 'config': {'database': dynamic_sqlite_db},
            'credentials': {}, 'is_public': public, 'generate_summary': False,
        })
        assert result.status_code == 200, result.text
        agents.append(result.json()['id'])
    app = test_client.post('/api/connected_applications', headers=headers, json={
        'name': 'Limited external application', 'agent_ids': [agents[0], agents[2]],
    }).json()
    client = test_client.post('/api/oauth/clients', headers=headers, json={
        'application_id': app['id'], 'name': app['name'], 'scopes': 'app mcp', 'redirect_uris': [APP_REDIRECT_URI], 'trusted': True,
    }).json()
    token = admin_token
    if role == 'member':
        email = f'app-ceiling-{secrets.token_hex(6)}@example.com'
        assert test_client.post(f'/api/organizations/{org}/members', headers=headers, json={'organization_id': org, 'email': email, 'role': 'member'}).status_code == 200
        member = create_user(email=email)
        token = login_user(member['email'], member['password'])
    oauth = _issue_token(test_client, login_token=token, client=client, scope='app mcp')
    access = {'Authorization': f"Bearer {oauth['access_token']}", 'X-Organization-Id': org}
    expected = {agents[0], agents[2]} if role == 'admin' else {agents[0]}
    listed = test_client.get('/api/data_sources?show_all=true', headers=access)
    assert listed.status_code == 200, listed.text
    assert {a['id'] for a in listed.json()} == expected
    for agent_id in agents:
        response = test_client.get('/api/data_sources/' + agent_id, headers=access)
        assert response.status_code == (200 if agent_id in expected else 403), response.text
    import json
    enable_mcp(user_token=admin_token, org_id=org)
    for agent_id in agents:
        response = test_client.post('/api/mcp', headers=access, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'describe_tables', 'arguments': {'data_source_id': agent_id}},
        })
        assert response.status_code == 200, response.text
        result = json.loads(response.json()['result']['content'][0]['text'])
        assert result['success'] == (agent_id in expected), result
    # A restricted token cannot grant itself additional agents.
    assert test_client.patch('/api/connected_applications/' + app['id'], headers=access, json={'agent_ids': None}).status_code == 403
    # Live edits also affect tokens issued before the edit.
    assert test_client.patch('/api/connected_applications/' + app['id'], headers=headers, json={'agent_ids': []}).status_code == 200
    assert test_client.get('/api/data_sources?show_all=true', headers=access).json() == []
    assert test_client.get('/api/data_sources/' + agents[0], headers=access).status_code == 403
