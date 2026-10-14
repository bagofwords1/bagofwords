"""Synthetic HTTP; real tool, resolver, credential handling and clients."""
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from app.ai.tools.implementations.list_files import ListFilesTool
from app.models.connection import Connection
from tests.unit.test_sharepoint_2010_client import legacy, ROOT, SITE
from tests.unit.test_sharepoint_onprem_client import remote


def runtime(kind, **config):
    graph = kind == 'graph'
    conn = Connection(id='synthetic-connection', name='Synthetic documents',
                      type='sharepoint' if graph else 'sharepoint_onprem', is_active=True,
                      auth_policy='user_required' if graph else 'system_only',
                      allowed_user_auth_modes=['oauth'] if graph else [],
                      config={'site_url': SITE, 'drive_name': 'Documents',
                              **({} if graph else {'api_mode': kind}), **config})
    conn.encrypt_credentials({'access_token': 'synthetic-token'} if graph else
                             {'username': 'DOMAIN\\reader', 'password': 'synthetic'})
    ds = SimpleNamespace(id='synthetic-agent', name='Synthetic', is_active=True, connections=[conn])
    # Transient models: the system-context resolution path does not access the DB.
    return {'db': object(), 'organization': SimpleNamespace(id='synthetic-org'),
            'report': SimpleNamespace(data_sources=[ds])}


async def run(kind, *, inputs=None, config=None):
    events = [e async for e in ListFilesTool().run_stream(
        {'connection_id': 'synthetic-connection', **(inputs or {})}, runtime(kind, **(config or {})))]
    return events[-1].payload


@pytest.fixture
def graph(monkeypatch):
    from app.data_sources.clients import graph_drive_client as gdc
    from app.data_sources.clients._graph_throttle import listing_cache, reset_throttle_states
    listing_cache.clear()
    reset_throttle_states()
    monkeypatch.setattr(gdc, 'GRAPH_USE_DELTA', False)
    state = {'calls': [], 'status': 200, 'pages': 0}
    files = [{'id': f'file-{i}', 'name': f'{i:03}.csv', 'file': {}, 'size': 1} for i in range(12)]
    state['tree'] = {'root': files + [{'id': 'nested', 'name': 'nested', 'folder': {}}],
                     'nested': [{'id': 'deep', 'name': 'deep.csv', 'file': {}, 'size': 1}]}

    def send(session, req, **kwargs):
        state['calls'].append(req)
        path = unquote(req.url.path)
        if state['status'] != 200:
            return httpx.Response(state['status'], json={'error': 'denied'}, request=req)
        if path.endswith('/me/drive'):
            data = {'id': 'drive', 'name': 'Documents'}
        elif '/sites/portal.example.com:' in path:
            data = {'id': 'site'}
        elif path.endswith('/sites/site/drives'):
            data = {'value': state.get('drives', [{'id': 'drive', 'name': 'Documents'}])}
        elif path.endswith('/root') or '/root:/' in path:
            data = {'id': 'root'}
        elif path.endswith('/items/nested'):
            data = state.get('folder', {'id': 'nested', 'name': 'nested', 'folder': {},
                                       'parentReference': {'path': '/drives/drive/root:'}})
        elif path.endswith('/children'):
            state['pages'] += 1
            folder = path.split('/items/')[1].split('/')[0]
            params = parse_qs(req.url.query.decode())
            offset = int(params.get('offset', ['0'])[0])
            size = min(2, int(params.get('$top', ['200'])[0]))
            values = state['tree'][folder]
            data = {'value': values[offset:offset + size]}
            if offset + size < len(values):
                data['@odata.nextLink'] = str(req.url.copy_set_param('offset', str(offset + size)))
            if state.get('next'):
                data['@odata.nextLink'] = state['next']
        else:
            raise AssertionError(f'Unexpected synthetic Graph path: {path}')
        return httpx.Response(200, json=data, request=req)

    monkeypatch.setattr(httpx.Client, 'send', send)
    yield state
    listing_cache.clear()
    reset_throttle_states()


@pytest.fixture(params=['2010', 'modern', 'graph'])
def provider(request):
    kind = request.param
    server = request.getfixturevalue({'2010': 'legacy', 'modern': 'remote', 'graph': 'graph'}[kind])
    if kind != 'graph':
        blobs = {ROOT + f'/{i:03}.csv': b'x\n1\n' for i in range(12)}
        blobs[ROOT + '/nested/deep.csv'] = b'x\n2\n'
        if kind == '2010':
            server.blobs = blobs
            server.empty_folders.clear()
        else:
            server['blobs'].clear()
            server['blobs'].update(blobs)
            server['page_size'] = 2
    return kind, server


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 3])
async def test_agent_limits_matching_files_before_draining_pages(provider, limit):
    kind, server = provider
    result = await run(kind, inputs={'limit': limit})
    out = result['output']
    assert out['success'], result
    assert len(out['files']) == limit
    assert out['incomplete'] and out['stop_reason'] == 'result_limit'
    if kind == '2010':
        assert server.calls.count('GetListItems') <= (limit + 1) // 2
    elif kind == 'graph':
        assert server['pages'] <= (limit + 1) // 2
    else:
        assert sum('/Files' in url for url, _ in server['calls']) <= (limit + 1) // 2


@pytest.mark.asyncio
async def test_agent_honors_recursion_and_filters_before_limit(provider):
    kind, server = provider
    out = (await run(kind, inputs={'limit': 1, 'name_pattern': '*DEEP*'}))['output']
    assert out['success'] and out['files'] == []
    out = (await run(kind, inputs={'limit': 1, 'name_pattern': '*DEEP*', 'recursive': True}))['output']
    assert out['success'] and len(out['files']) == 1
    assert out['files'][0]['path'] == 'nested/deep.csv'


@pytest.mark.asyncio
async def test_agent_late_filename_match_is_not_hidden_by_limit(provider):
    kind, server = provider
    out = (await run(kind, inputs={'limit': 1, 'name_pattern': '*011.csv'}))['output']
    assert out['success'] and [f['name'] for f in out['files']] == ['011.csv']


@pytest.mark.asyncio
async def test_agent_reports_connection_cap_without_claiming_exhaustion(provider):
    kind, server = provider
    out = (await run(kind, inputs={'limit': 10, 'name_pattern': '*missing*'},
                     config={'max_catalog_objects': 3}))['output']
    assert out['success'] and out['files'] == []
    assert out['incomplete'] and out['stop_reason'] == 'catalog_limit'


@pytest.mark.asyncio
async def test_agent_never_returns_shared_inventory_on_access_denied(provider):
    kind, server = provider
    if kind == '2010':
        server.status = 403
    else:
        server['status'] = 403
    out = (await run(kind, inputs={'limit': 1}))['output']
    assert not out['success']
    assert not out.get('files')


@pytest.mark.asyncio
async def test_small_complete_listing_is_not_marked_incomplete(provider):
    kind, _ = provider
    out = (await run(kind, inputs={'limit': 50}))['output']
    assert out['success'] and len(out['files']) == 12
    assert out['incomplete'] is False and out['stop_reason'] is None


@pytest.mark.asyncio
async def test_default_limit_is_fifty(provider):
    kind, server = provider
    if kind == 'graph':
        server['tree']['root'] = [
            {'id': f'item-{i}', 'name': f'{i:03}.txt', 'file': {}} for i in range(70)]
    else:
        blobs = {ROOT + f'/{i:03}.txt': b'text' for i in range(70)}
        if kind == '2010':
            server.blobs = blobs
        else:
            server['blobs'].clear()
            server['blobs'].update(blobs)
    out = (await run(kind))['output']
    assert out['success'] and len(out['files']) == 50
    assert out['incomplete']


@pytest.mark.asyncio
async def test_connection_globs_and_extensions_precede_interactive_filter(provider):
    kind, _ = provider
    out = (await run(kind, inputs={'limit': 1, 'name_pattern': '*'},
                     config={'include_globs': '*011.csv', 'allowed_extensions': 'csv'}))['output']
    assert out['success'] and [f['name'] for f in out['files']] == ['011.csv']
    out = (await run(kind, inputs={'limit': 1}, config={'allowed_extensions': 'pdf'}))['output']
    assert out['success'] and out['files'] == []


@pytest.mark.asyncio
async def test_other_file_connectors_keep_their_existing_listing_contract(tmp_path):
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'root.txt').write_text('synthetic')
    (tmp_path / 'nested' / 'child.txt').write_text('synthetic')
    ctx = runtime('modern')
    conn = ctx['report'].data_sources[0].connections[0]
    conn.type = 'network_dir'
    conn.config = {'root_path': str(tmp_path), 'writable': False}
    conn.encrypt_credentials({})
    events = [e async for e in ListFilesTool().run_stream(
        {'connection_id': conn.id, 'limit': 1, 'recursive': False}, ctx)]
    out = events[-1].payload['output']
    # SharePoint's new limit/recursion contract must not alter the existing
    # no-folder behavior or pass unsupported kwargs to this real connector.
    assert out['success'] and {f['path'] for f in out['files']} == {'root.txt', 'nested/child.txt'}


@pytest.mark.asyncio
async def test_onedrive_keeps_its_existing_listing_path(graph):
    ctx = runtime('graph')
    conn = ctx['report'].data_sources[0].connections[0]
    conn.type = 'onedrive'
    events = [e async for e in ListFilesTool().run_stream(
        {'connection_id': conn.id, 'limit': 1}, ctx)]
    out = events[-1].payload['output']
    assert out['success'] and len(out['files']) == 13


@pytest.mark.parametrize('limit', [0, -1, 501])
def test_agent_limit_validation(limit):
    from app.ai.tools.schemas.file_tools import ListFilesInput
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ListFilesInput(connection_id='synthetic', limit=limit)


@pytest.mark.asyncio
async def test_limits_apply_across_libraries(provider):
    kind, server = provider
    if kind == 'graph':
        server['drives'] = [{'id': 'drive', 'name': 'Documents'}, {'id': 'other', 'name': 'Policies'}]
    out = (await run(kind, inputs={'limit': 1, 'recursive': True}, config={'drive_name': '*'}))['output']
    assert out['success'] and len(out['files']) == 1
    assert out['files'][0]['path'].startswith('Documents/')
    if kind == 'graph':
        assert not any('/drives/other/' in str(req.url) for req in server['calls'])


@pytest.mark.asyncio
async def test_explicit_graph_folder_uses_its_scoped_path(graph):
    out = (await run('graph', inputs={'folder_id': 'nested', 'limit': 1},
                     config={'include_globs': 'nested/*.csv'}))['output']
    assert out['success'] and out['files'][0]['path'] == 'nested/deep.csv'
    assert graph['pages'] == 1


@pytest.mark.asyncio
async def test_graph_folder_outside_connection_scope_is_rejected(graph):
    out = (await run('graph', inputs={'folder_id': 'nested', 'limit': 1},
                     config={'folder_path': 'Reports'}))['output']
    assert not out['success'] and not out.get('files')
    assert graph['pages'] == 0


@pytest.mark.asyncio
async def test_graph_foreign_drive_is_rejected_before_listing(graph):
    out = (await run('graph', inputs={'folder_id': 'other|nested', 'limit': 1}))['output']
    assert not out['success'] and graph['pages'] == 0


@pytest.mark.asyncio
async def test_graph_does_not_follow_foreign_paging_links(graph):
    graph['next'] = 'https://untrusted.example/collect'
    out = (await run('graph', inputs={'limit': 3}))['output']
    assert not out['success']
    assert all(req.url.host == 'graph.microsoft.com' for req in graph['calls'])


@pytest.mark.asyncio
async def test_interactive_listing_does_not_change_indexing(provider):
    from app.schemas.data_source_registry import resolve_client_class
    kind, _ = provider
    ctx = runtime(kind)
    conn = ctx['report'].data_sources[0].connections[0]
    client = resolve_client_class(conn.type)(**conn.config, **conn.decrypt_credentials())
    result = await client.alist_files_bounded(limit=1)
    assert len(result.files) == 1
    assert len(client.list_files(recursive=True)) == 13
    assert len(client.get_schemas()) == 12  # configured non-recursive indexing
