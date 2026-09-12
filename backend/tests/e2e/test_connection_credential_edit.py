"""Credential edits preserve omitted values and never expose secrets in metadata."""
import pytest
from pathlib import Path
from app.dependencies import async_session_maker
from app.models.connection import Connection

@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [{'client_secret': 'rotated'}, {'user': 'new-reader'}, {'password': '', 'tenant_id': 'new-tenant'}])
async def test_partial_credentials_preserve_saved_fields(patch, create_connection, get_connection, update_connection, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user['email'], user['password'])
    org = whoami(token)['organizations'][0]['id']
    original = dict(tenant_id='tenant-a', client_id='app-a', user='reader', password='saved-password', client_secret='saved-secret', oauth_client_id='oauth-app', oauth_client_secret='oauth-secret')
    # SQLite is a real local driver; credential storage is independent of source type.
    conn = create_connection(name='Credential edit', type='sqlite', config={'database': str(Path(__file__).resolve().parents[1] / 'config' / 'chinook.sqlite')}, credentials=original, user_token=token, org_id=org)
    update_connection(connection_id=conn['id'], payload={'credentials': patch}, user_token=token, org_id=org)
    async with async_session_maker() as db:
        row = await db.get(Connection, conn['id'])
        assert row.decrypt_credentials() == {**original, **{k:v for k,v in patch.items() if v not in ('', None)}}
    detail = get_connection(connection_id=conn['id'], user_token=token, org_id=org)
    meta = detail['credentials_meta']
    for key in ('tenant_id', 'client_id', 'user', 'oauth_client_id'):
        assert meta[key] == patch.get(key, original[key])
    assert not {'password','client_secret','oauth_client_secret'} & meta.keys()
