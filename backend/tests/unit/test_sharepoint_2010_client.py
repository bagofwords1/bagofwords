"""Synthetic SOAP responses based on MS-LISTSWS and MS-COPYS; mock HTTP only."""
import base64
import io
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
import pytest
import requests
from app.data_sources.clients.sharepoint_onprem_client import SharepointOnpremClient, SharePointHTTPError
from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
SP = 'http://schemas.microsoft.com/sharepoint/soap/'
RS = 'urn:schemas-microsoft-com:rowset'
SITE = 'https://portal.example.com/sites/research'
ROOT = '/sites/research/Shared Documents'
OTHER = '/sites/research/Policies'

def response(content, status=200):
    r = requests.Response()
    r.status_code, r.raw = status, io.BytesIO(content)
    r.headers['Content-Length'] = str(len(content))
    return r

def envelope(operation):
    root = ET.Element(f'{{{SOAP}}}Envelope')
    return root, ET.SubElement(ET.SubElement(root, f'{{{SOAP}}}Body'), f'{{{SP}}}{operation}Response')

class LegacyServer:
    def __init__(self):
        self.blobs = {ROOT + '/revenue.csv': b'region,amount\nNorth,120\nSouth,80\n', ROOT + '/nested/דוח.txt': 'שלום'.encode(), ROOT + "/O'Brien & costs.txt": b'known contents', ROOT + '/empty.txt': b'', OTHER + '/rules.txt': b'Policy'}
        self.libraries = [('Documents', ROOT, 'lib1'), ('Policies', OTHER, 'lib2')]
        self.library_attributes = {}
        self.collection_has_roots = True
        self.calls, self.identities = [], []
        self.status, self.override, self.page_size = None, None, 2
        self.empty_folders = {ROOT + '/empty-folder'}

    def handle(self, method, url, data=None):
        if method != 'POST':
            return b'No modern REST API on SharePoint 2010', 404
        command = ET.fromstring(data).find(f'{{{SOAP}}}Body')[0]
        op = command.tag.split('}')[-1]
        assert url.endswith('/_vti_bin/' + ('Copy.asmx' if op == 'GetItem' else 'Lists.asmx'))
        self.calls.append(op)
        if self.override:
            result = self.override(op, command)
            if result is not None:
                return result
        root, out = envelope(op)
        if op == 'GetListCollection':
            lists = ET.SubElement(ET.SubElement(out, f'{{{SP}}}GetListCollectionResult'), f'{{{SP}}}Lists')
            for title, path, guid in self.libraries:
                attrs = dict(ID=guid, Title=title, RootFolder=path, BaseType='1', ServerTemplate='101', Hidden='False')
                attrs.update(self.library_attributes.get(guid, {}))
                if not self.collection_has_roots:
                    attrs.pop('RootFolder')
                ET.SubElement(lists, f'{{{SP}}}List', **attrs)
        elif op == 'GetList':
            name = command.findtext(f'{{{SP}}}listName')
            lib = next((x for x in self.libraries if name.casefold() in (x[0].casefold(), x[2])), None)
            if not lib:
                fault = ET.Element(f'{{{SOAP}}}Envelope')
                f = ET.SubElement(ET.SubElement(fault, f'{{{SOAP}}}Body'), f'{{{SOAP}}}Fault')
                ET.SubElement(ET.SubElement(f, 'detail'), f'{{{SP}}}errorcode').text = '0x82000006'
                return ET.tostring(fault), 500
            title, path, guid = lib
            attrs = dict(ID=guid, Title=title, RootFolder=path, BaseType='1', ServerTemplate='101', Hidden='False')
            attrs.update(self.library_attributes.get(guid, {}))
            node = ET.SubElement(ET.SubElement(out, f'{{{SP}}}GetListResult'), f'{{{SP}}}List', **attrs)
            ET.SubElement(node, f'{{{SP}}}Validation').text = 'Extra schema element'
            ET.SubElement(node, f'{{{SP}}}Fields')
        elif op == 'GetListItems':
            guid = command.findtext(f'{{{SP}}}listName')
            libroot = next(x[1] for x in self.libraries if x[2] == guid)
            values = {x.tag.split('}')[-1]: x for x in command.iter()}
            folder = values['Folder'].text or libroot
            limit = int(command.findtext(f'{{{SP}}}rowLimit'))
            assert limit > 0
            assert {'FileRef', 'FileLeafRef', 'FSObjType', 'File_x0020_Size'} <= {x.attrib['Name'] for x in values['ViewFields']}
            token = values.get('Paging')
            offset = int(token.attrib['ListItemCollectionPositionNext'].split('p_ID=')[1]) if token is not None else 0
            directories = set(self.empty_folders)
            for path in self.blobs:
                parent = path.rsplit('/', 1)[0]
                while parent.startswith(libroot + '/'):
                    directories.add(parent)
                    parent = parent.rsplit('/', 1)[0]
            children = sorted(p for p in set(self.blobs) | directories if p.rsplit('/', 1)[0].casefold() == folder.casefold())
            # MS-LISTSWS Query uses CAML; apply the documented exact leaf filter.
            where = command.find(f'{{{SP}}}query/Query/Where')
            if where is not None:
                eq = where.find('Eq')
                assert eq is not None and eq.find('FieldRef').get('Name') == 'FileLeafRef'
                assert eq.find('Value').get('Type') == 'Text'
                leaf = eq.findtext('Value')
                children = [p for p in children if p.rsplit('/', 1)[1].casefold() == leaf.casefold()]
            page = children[offset:offset + min(limit, self.page_size)]
            items = ET.SubElement(ET.SubElement(out, f'{{{SP}}}GetListItemsResult'), f'{{{SP}}}listitems')
            rows = ET.SubElement(items, f'{{{RS}}}data', ItemCount=str(len(page)))
            if offset + len(page) < len(children):
                rows.set('ListItemCollectionPositionNext', f'Paged=TRUE&p_ID={offset + len(page)}')
            for path in page:
                ET.SubElement(rows, '{#RowsetSchema}row', ows_ID='7', ows_FileRef='7;#' + path.lstrip('/'), ows_FileLeafRef='7;#' + path.rsplit('/', 1)[1], ows_FSObjType='7;#' + str(int(path in directories)), ows_File_x0020_Size='7;#' + str(len(self.blobs.get(path, b''))), ows_Modified='2026-01-02T03:04:05Z', ows_Created='2025-12-31T23:00:00Z')
        elif op == 'GetItem':
            path = unquote(urlsplit(command.findtext(f'{{{SP}}}Url')).path)
            ET.SubElement(out, f'{{{SP}}}GetItemResult').text = '0'
            if path in self.blobs:
                ET.SubElement(out, f'{{{SP}}}Fields')
                ET.SubElement(out, f'{{{SP}}}Stream').text = base64.b64encode(self.blobs[path]).decode()
        else:
            raise AssertionError(f'Unexpected SOAP operation {op}')
        return ET.tostring(root), self.status or 200

@pytest.fixture
def legacy(monkeypatch):
    server = LegacyServer()
    def send(session, method, url, **kwargs):
        server.identities.append(getattr(session.auth, 'username', None))
        assert kwargs.get('allow_redirects') is False
        assert session.verify is True and session.trust_env is False
        body, status = server.handle(method, url, kwargs.get('data'))
        return response(body, status)
    monkeypatch.setattr(requests.Session, 'get', lambda s, url, **kw: send(s, 'GET', url, **kw))
    monkeypatch.setattr(requests.Session, 'post', lambda s, url, **kw: send(s, 'POST', url, **kw))
    return server

def client(**kwargs):
    return SharepointOnpremClient(site_url=SITE, username='DOMAIN\\reader', password='synthetic', api_mode='2010', **kwargs)

def test_legacy_connection_lists_and_reads_documents(legacy):
    c = client(drive_name='Documents', recursive=True)
    assert c.test_connection()['success']
    files = c.list_files()
    assert {f['id'] for f in files} == {p for p in legacy.blobs if p.startswith(ROOT + '/')}
    for entry in files:
        content, name, _ = c.read_raw_bytes(entry['id'])
        assert content == legacy.blobs[entry['id']]
        assert name == entry['name'] and entry['size'] == len(content)
    assert c.read_file('nested/דוח.txt') == 'שלום'
    assert set(legacy.identities) == {'DOMAIN\\reader'}

@pytest.mark.parametrize('template', ['12042', '13057'])
@pytest.mark.parametrize('library', ['*', 'Documents'])
@pytest.mark.parametrize('collection_has_roots', [True, False])
def test_custom_document_libraries_support_catalog_search_and_reads(
    legacy, template, library, collection_has_roots,
):
    legacy.library_attributes['lib1'] = {'ServerTemplate': template}
    legacy.collection_has_roots = collection_has_roots
    c = client(drive_name=library, recursive=True)
    expected = {p for p in legacy.blobs if library == '*' or p.startswith(ROOT + '/')}
    files = c.list_files()
    assert {f['id'] for f in files} == expected
    assert {table.name for table in c.get_schemas()} == {f['path'] for f in files}
    assert {f['id'] for f in c.search_files('revenue')} == {ROOT + '/revenue.csv'}
    for path in expected:
        assert c.read_raw_bytes(path)[0] == legacy.blobs[path]

@pytest.mark.parametrize('attributes', [
    {'Hidden': 'True'}, {'BaseType': '0'}, {'BaseType': ''}, {'Hidden': ''},
])
def test_hidden_and_non_document_libraries_are_not_accessible(legacy, attributes):
    legacy.library_attributes['lib1'] = attributes
    c = client(recursive=True)
    assert {f['id'] for f in c.list_files()} == {p for p in legacy.blobs if p.startswith(OTHER + '/')}
    with pytest.raises(ValueError):
        c.read_raw_bytes(ROOT + '/revenue.csv')
    selected = client(drive_name='Documents')
    assert not selected.test_connection()['success']
    with pytest.raises(ValueError):
        selected.read_raw_bytes(ROOT + '/revenue.csv')
    assert 'GetItem' not in legacy.calls

@pytest.mark.parametrize('library,recursive', [('Documents', False), ('Documents', True), ('*', False), ('*', True)])
def test_listing_scope_pagination_and_catalog_agree(legacy, library, recursive):
    c = client(drive_name=library, recursive=recursive)
    files = c.list_files()
    roots = [ROOT, OTHER] if library == '*' else [ROOT]
    expected = {p for p in legacy.blobs if any(p.startswith(r + '/') and (recursive or p.rsplit('/', 1)[0] == r) for r in roots)}
    assert {f['id'] for f in files} == expected
    assert {t.name for t in c.get_schemas()} == {f['path'] for f in files}
    assert all(f['web_url'].startswith(SITE + '/') and not f['is_folder'] for f in files)

@pytest.mark.parametrize('folder', ['nested', 'empty-folder'])
def test_scoped_folder_and_empty_folder_are_valid(legacy, folder):
    c = client(drive_name='Documents', folder_path=folder, recursive=True)
    assert c.test_connection()['success']
    assert {f['id'] for f in c.list_files()} == {p for p in legacy.blobs if p.startswith(ROOT + '/' + folder + '/')}
    with pytest.raises(GlobScopeError):
        c.read_file(ROOT + '/revenue.csv')

def test_missing_folder_library_and_file_fail(legacy):
    assert not client(drive_name='missing').test_connection()['success']
    assert not client(drive_name='Documents', folder_path='missing').test_connection()['success']
    with pytest.raises(ValueError):
        client(drive_name='Documents').read_file('missing.txt')

def test_all_libraries_skip_only_missing_scoped_folders(legacy):
    files = client(folder_path='nested').list_files()
    assert {f['id'] for f in files} == {ROOT + '/nested/דוח.txt'}

def test_filters_do_not_stop_at_empty_matching_pages(legacy):
    c = client(drive_name='Documents', include_globs='**/*.csv')
    files = c.list_files()
    assert [f['id'] for f in files] == [ROOT + '/revenue.csv']
    assert len(c.read_file(files[0]['id'])) == 2
    with pytest.raises(GlobScopeError):
        c.read_file(ROOT + '/empty.txt')

@pytest.mark.parametrize('limit', [0, 1, 2])
def test_listing_limit_and_empty_search(legacy, limit):
    assert len(client(recursive=True).list_files(limit=limit)) == limit
    assert client().search_files('') == []
    assert client().search_files('revenue', limit=0) == []

def test_search_is_scoped_filename_search(legacy):
    c = client(drive_name='Documents', recursive=True)
    assert [f['name'] for f in c.search_files('דוח')] == ['דוח.txt']
    assert c.search_files('Policy') == []
    assert 'does not search document contents' in c.description

@pytest.mark.parametrize('path', ['../Policies/rules.txt', ROOT + '/../Policies/rules.txt', ROOT + '/%2e%2e/Policies/rules.txt', '//other.example/file.txt'])
def test_traversal_never_downloads(legacy, path):
    with pytest.raises(ValueError):
        client(drive_name='Documents').read_file(path)
    assert 'GetItem' not in legacy.calls

def test_qualified_paths_and_ambiguous_names(legacy):
    legacy.blobs[OTHER + '/empty.txt'] = b'Other'
    c = client()
    assert c.read_raw_bytes('Documents/empty.txt')[0] == b''
    assert c.read_raw_bytes('Policies/empty.txt')[0] == b'Other'
    with pytest.raises(ValueError):
        c.read_raw_bytes('empty.txt')

def test_file_size_limit_prevents_download(legacy):
    with pytest.raises(FileTooLargeError):
        client(drive_name='Documents').read_raw_bytes('revenue.csv', max_bytes=1)
    assert 'GetItem' not in legacy.calls

@pytest.mark.parametrize('failure', ['missing', 'invalid', 'oversize', 'fault', 'denied'])
def test_download_response_failures_are_not_success(legacy, failure):
    def override(op, command):
        if op != 'GetItem':
            return None
        root, out = envelope(op)
        ET.SubElement(out, f'{{{SP}}}GetItemResult').text = '0'
        if failure == 'invalid':
            ET.SubElement(out, f'{{{SP}}}Stream').text = 'not base64!'
        elif failure == 'oversize':
            ET.SubElement(out, f'{{{SP}}}Stream').text = base64.b64encode(b'x' * 100).decode()
        elif failure == 'fault':
            body = root.find(f'{{{SOAP}}}Body')
            body.remove(out)
            ET.SubElement(ET.SubElement(body, f'{{{SOAP}}}Fault'), 'faultstring').text = 'secret provider data'
        return ET.tostring(root), 401 if failure == 'denied' else 200
    legacy.override = override
    with pytest.raises(ValueError) as err:
        client(drive_name='Documents').read_raw_bytes('empty.txt', max_bytes=50)
    assert 'secret provider data' not in str(err.value)

@pytest.mark.parametrize('status', [401, 403, 404, 429, 500, 302])
def test_http_failures_are_visible(legacy, status):
    legacy.override = lambda *args: (b'private provider response', status)
    result = client(drive_name='Documents').test_connection()
    assert not result['success']
    assert 'private provider response' not in result['message']

@pytest.mark.parametrize('xml', [b'<html>Login</html>', b'<broken', b'<!DOCTYPE x [<!ENTITY e "secret">]><x>&e;</x>'])
def test_invalid_and_unsafe_xml_fails_closed(legacy, xml):
    legacy.override = lambda *args: (xml, 200)
    assert not client(drive_name='Documents').test_connection()['success']

def test_pagination_cycle_fails_instead_of_partial_success(legacy):
    def override(op, command):
        if op != 'GetListItems':
            return None
        root, out = envelope(op)
        rows = ET.SubElement(ET.SubElement(ET.SubElement(out, f'{{{SP}}}GetListItemsResult'), f'{{{SP}}}listitems'), f'{{{RS}}}data', ItemCount='0', ListItemCollectionPositionNext='repeat')
        return ET.tostring(root), 200
    legacy.override = override
    with pytest.raises(ValueError):
        client(drive_name='Documents').list_files()

def test_folder_browse_stays_inside_scope(legacy):
    c = client(drive_name='Documents')
    assert {f['id'] for f in c.list_files(folder_id=ROOT + '/nested')} == {ROOT + '/nested/דוח.txt'}
    with pytest.raises(GlobScopeError):
        c.list_files(folder_id=OTHER)

@pytest.mark.parametrize('mode', ['2013', 'auto', ''])
def test_unknown_api_mode_rejected(mode):
    with pytest.raises(ValueError):
        SharepointOnpremClient(site_url=SITE, username='test', password='test', api_mode=mode)

def test_legacy_requires_explicit_library():
    with pytest.raises(ValueError):
        client(drive_name='')

def test_configuration_default_selector_and_validation():
    from app.schemas.data_sources.configs import SharePointOnpremConfig
    from app.schemas.data_source_registry import resolve_client_class
    assert SharePointOnpremConfig(site_url=SITE).api_mode == 'modern'
    assert resolve_client_class('sharepoint_onprem') is SharepointOnpremClient
    schema = SharePointOnpremConfig.model_json_schema()['properties']['api_mode']
    assert set(schema['enum']) == {'modern', '2010'}
    for invalid in ('', None, ' '):
        with pytest.raises(ValueError):
            SharePointOnpremConfig(site_url=SITE, api_mode='2010', drive_name=invalid)
    with pytest.raises(ValueError):
        SharePointOnpremConfig(site_url=SITE, api_mode='auto')

def test_empty_library_is_a_successful_connection(legacy):
    legacy.blobs.clear()
    legacy.empty_folders.clear()
    c = client(drive_name='Documents')
    assert c.test_connection()['success']
    assert c.list_files() == []

def test_legacy_uses_kerberos_session_without_ntlm_fallback(legacy, monkeypatch):
    import sys
    import types
    acquired = []
    class Creds:
        lifetime = 3600
        def __init__(self, **kw):
            acquired.append(kw)
    auths = []
    def auth(**kw):
        auths.append(kw)
        return object()
    monkeypatch.setitem(sys.modules, 'gssapi', types.SimpleNamespace(Credentials=Creds, Name=lambda n, t: n,
        NameType=types.SimpleNamespace(kerberos_principal='principal'), MechType=types.SimpleNamespace(kerberos='kerberos')))
    monkeypatch.setitem(sys.modules, 'requests_gssapi', types.SimpleNamespace(HTTPSPNEGOAuth=auth, REQUIRED='required'))
    c = SharepointOnpremClient(site_url=SITE, api_mode='2010', drive_name='Documents', kerberos=True, principal='svc@EXAMPLE.COM')
    assert c.test_connection()['success']
    assert acquired and auths[0]['mech'] == 'kerberos'
    assert set(legacy.identities) == {None}

def test_transport_retry_reacquires_session(legacy, monkeypatch):
    original = requests.Session.post
    sessions = []
    def retry(session, *args, **kw):
        sessions.append(session)
        if len(sessions) == 1:
            raise requests.ConnectionError('synthetic broken connection')
        return original(session, *args, **kw)
    monkeypatch.setattr(requests.Session, 'post', retry)
    assert client(drive_name='Documents').test_connection()['success']
    assert sessions[0] is not sessions[1]

def test_tls_failure_is_not_retried(legacy, monkeypatch):
    calls = []
    def fail(*args, **kw):
        calls.append(1)
        raise requests.exceptions.SSLError('private provider details')
    monkeypatch.setattr(requests.Session, 'post', fail)
    result = client(drive_name='Documents').test_connection()
    assert not result['success'] and len(calls) == 1
    assert 'private provider details' not in result['message']

@pytest.mark.parametrize('field,value', [
    ('ows_FileRef', '9;#sites/other/private.txt'),
    ('ows_FileRef', '9;#sites/research/Shared Documents/../Policies/private.txt'),
    ('ows_FileLeafRef', 'mismatched.txt'),
    ('ows_FSObjType', '9;#unknown'),
    ('ows_File_x0020_Size', '-1'),
])
def test_invalid_document_metadata_never_becomes_a_readable_file(legacy, field, value):
    def override(op, command):
        if op != 'GetListItems':
            return None
        root, out = envelope(op)
        rows = ET.SubElement(ET.SubElement(ET.SubElement(out, f'{{{SP}}}GetListItemsResult'), f'{{{SP}}}listitems'), f'{{{RS}}}data', ItemCount='1')
        row = ET.SubElement(rows, '{#RowsetSchema}row', ows_FileRef='9;#' + ROOT.lstrip('/') + '/file.txt',
            ows_FileLeafRef='9;#file.txt', ows_FSObjType='9;#0', ows_File_x0020_Size='9;#10')
        row.set(field, value)
        return ET.tostring(root), 200
    legacy.override = override
    with pytest.raises(ValueError):
        client(drive_name='Documents').list_files()
    assert 'GetItem' not in legacy.calls


def test_collection_without_root_folder_resolves_list_metadata(legacy):
    def override(op, command):
        if op == 'GetListCollection':
            return (f'''<s:Envelope xmlns:s="{SOAP}"><s:Body>
            <GetListCollectionResponse xmlns="{SP}"><GetListCollectionResult><Lists>
            <List ID="lib1" Title="Documents" BaseType="1" ServerTemplate="101" Hidden="False" />
            <List ID="hidden" Title="Hidden" BaseType="1" ServerTemplate="101" Hidden="True" />
            <List ID="list" Title="Tasks" BaseType="0" ServerTemplate="100" Hidden="False" />
            </Lists></GetListCollectionResult></GetListCollectionResponse>
            </s:Body></s:Envelope>'''.encode(), 200)
    legacy.override = override
    c = client(recursive=True)
    files = c.list_files()
    assert {f['id'] for f in files} == {p for p in legacy.blobs if p.startswith(ROOT + '/')}
    assert c.list_files() == files
    assert legacy.calls.count('GetListCollection') == 1
    assert legacy.calls.count('GetList') == 1


@pytest.mark.parametrize('header', [True, False])
def test_streamed_response_cap_is_enforced_with_or_without_length(legacy, monkeypatch, header):
    original = requests.Session.post
    def oversized(session, url, **kwargs):
        if url.endswith('/Copy.asmx'):
            result = response(b'x' * (1024 * 1024 + 200))
            if not header:
                del result.headers['Content-Length']
            return result
        return original(session, url, **kwargs)
    monkeypatch.setattr(requests.Session, 'post', oversized)
    with pytest.raises(FileTooLargeError):
        client(drive_name='Documents').read_raw_bytes('empty.txt', max_bytes=1)


def test_copy_response_default_namespace_whitespace_and_ignored_result(legacy):
    def override(op, command):
        if op == 'GetItem':
            # MS-COPYS defines result as ignored; Stream determines the bytes.
            return (f'''<s:Envelope xmlns:s="{SOAP}"><s:Body>
            <GetItemResponse xmlns="{SP}"><GetItemResult>0</GetItemResult>
            <Fields/><Stream> c2FtcGxl\nIHRleHQuDQo= </Stream></GetItemResponse>
            </s:Body></s:Envelope>'''.encode(), 200)
    legacy.override = override
    assert client(drive_name='Documents').read_raw_bytes('empty.txt')[0] == b'sample text.\r\n'


@pytest.mark.parametrize('library', [' Documents ', '\tDocuments\t', ' * '])
def test_library_input_whitespace_is_normalized(legacy, library):
    from app.schemas.data_sources.configs import SharePointOnpremConfig
    config = SharePointOnpremConfig(site_url=SITE, api_mode='2010', drive_name=library)
    assert config.drive_name == library.strip()
    assert client(drive_name=library).test_connection()['success']

@pytest.mark.parametrize('folder', ['nested//deep', '/nested///deep/'])
def test_configured_folder_separators_are_normalized(legacy, folder):
    legacy.blobs[ROOT + '/nested/deep/data.txt'] = b'data'
    files = client(drive_name='Documents', folder_path=folder).list_files()
    assert {f['id'] for f in files} == {ROOT + '/nested/deep/data.txt'}

@pytest.mark.parametrize('header', [True, False])
def test_metadata_response_limit_is_not_a_file_size_error(legacy, monkeypatch, header):
    def oversized(*args, **kwargs):
        r = response(b'x' * (8 * 1024 * 1024 + 1))
        if not header:
            del r.headers['Content-Length']
        return r
    monkeypatch.setattr(requests.Session, 'post', oversized)
    with pytest.raises(ValueError) as caught:
        client(drive_name='Documents').list_files()
    assert not isinstance(caught.value, FileTooLargeError)
    assert 'metadata' in str(caught.value).lower()

@pytest.mark.parametrize('missing', ['ID', 'Title'])
def test_incomplete_library_metadata_is_a_domain_error(legacy, missing):
    def override(op, command):
        if op == 'GetList':
            root, out = envelope(op)
            attrs = dict(ID='lib1', Title='Documents', BaseType='1', ServerTemplate='101', Hidden='False')
            del attrs[missing]
            ET.SubElement(ET.SubElement(out, f'{{{SP}}}GetListResult'), f'{{{SP}}}List', **attrs)
            return ET.tostring(root), 200
    legacy.override = override
    with pytest.raises(ValueError, match='incomplete library metadata'):
        client(drive_name='Documents').list_files()

@pytest.mark.parametrize('count,limit', [(5000, 5), (900, 1)])
def test_search_stops_requesting_pages_when_limit_is_satisfied(legacy, count, limit):
    legacy.blobs = {ROOT + f'/match-{i:05}.txt': b'x' for i in range(count)}
    legacy.empty_folders.clear()
    legacy.page_size = 200
    results = client(drive_name='Documents', max_catalog_objects=50000).search_files('match-', limit=limit)
    assert len(results) == limit
    assert legacy.calls.count('GetListItems') == 1

@pytest.mark.parametrize('name', ["z-last & O'Brien.txt", 'דוח.txt'])
def test_exact_read_does_not_enumerate_parent_folders(legacy, name):
    legacy.blobs = {ROOT + f'/nested/deep/a-{i:05}.txt': b'x' for i in range(5000)}
    path = ROOT + '/nested/deep/' + name
    legacy.blobs[path] = b'target'
    legacy.page_size = 200
    assert client(drive_name='Documents').read_raw_bytes(path)[0] == b'target'
    assert legacy.calls.count('GetListItems') == 1


def test_library_metadata_is_reused_but_file_access_stays_live(legacy):
    c = client(drive_name='Documents')
    c.list_files(limit=1)
    c.search_files('revenue', limit=1)
    assert c.read_raw_bytes('revenue.csv')[0]
    assert legacy.calls.count('GetList') == 1
    legacy.status = 403
    with pytest.raises(ValueError):
        c.read_raw_bytes('revenue.csv')
    # A different caller gets its own discovery and authenticated requests.
    legacy.status = None
    client(drive_name='Documents').list_files(limit=1)
    assert legacy.calls.count('GetList') == 2


def _keywords(tables):
    return {t.name: t.metadata_json['sharepoint_onprem'] for t in tables}


def test_content_indexing_extracts_keywords_and_timestamps(legacy):
    c = client(drive_name='Documents', recursive=True, index_mode='content')
    rows = _keywords(c.get_schemas())
    assert set(rows) == {f['path'] for f in c.list_files()}
    for meta in rows.values():
        assert meta['indexed'] is True and meta['created_at'] and meta['modified_at']
    # Body tokens (not only filename tokens) are searchable, in any script.
    assert {'region', 'north', 'south'} <= set(rows['revenue.csv']['keywords'])
    assert 'שלום' in rows['nested/דוח.txt']['keywords']
    assert 'known' in rows["O'Brien & costs.txt"]['keywords']
    # Empty bodies are still findable by name.
    assert 'empty' in rows['empty.txt']['keywords'] and rows['empty.txt']['content_hash'] is None


def test_metadata_indexing_never_downloads_documents(legacy):
    rows = _keywords(client(drive_name='Documents', recursive=True, index_mode='metadata').get_schemas())
    assert rows and all('keywords' not in meta for meta in rows.values())
    assert all(meta['created_at'] for meta in rows.values())
    assert 'GetItem' not in legacy.calls


def test_incremental_reindex_downloads_only_changed_documents(legacy):
    c = client(drive_name='Documents', recursive=True, index_mode='content')
    first = c.get_schemas()
    downloads = legacy.calls.count('GetItem')
    assert downloads > 0

    prior = {t.name: t.metadata_json for t in first}
    second = _keywords(c.get_schemas(prior_catalog=prior))
    assert legacy.calls.count('GetItem') == downloads
    assert second == _keywords(first)

    # A changed document (different size) is re-read; the rest are reused.
    legacy.blobs[ROOT + '/revenue.csv'] = b'region,amount\nEast,500\n'
    third = _keywords(c.get_schemas(prior_catalog=prior))
    assert legacy.calls.count('GetItem') == downloads + 1
    assert 'east' in third['revenue.csv']['keywords'] and 'north' not in third['revenue.csv']['keywords']
    assert third['empty.txt'] == second['empty.txt']


def test_content_index_mode_is_reported_to_the_agent(legacy):
    assert 'keywords' in client(drive_name='Documents', index_mode='content').description
    assert 'does not search document contents' in client(drive_name='Documents').description


def test_failed_document_download_is_retried_on_the_next_run(legacy):
    c = client(drive_name='Documents', recursive=True, index_mode='content')
    failing = ROOT + '/revenue.csv'

    def throttle(op, command):
        if op == 'GetItem' and unquote(urlsplit(command.findtext(f'{{{SP}}}Url')).path) == failing:
            return b'<html>Service Unavailable</html>', 503
    legacy.override = throttle
    first = c.get_schemas()
    rows = _keywords(first)
    assert rows['revenue.csv']['indexed'] is False
    assert 'north' not in rows['revenue.csv']['keywords'] and 'revenue' in rows['revenue.csv']['keywords']
    assert all(meta['indexed'] for name, meta in rows.items() if name != 'revenue.csv')

    legacy.override = None
    downloads = legacy.calls.count('GetItem')
    second = _keywords(c.get_schemas(prior_catalog={t.name: t.metadata_json for t in first}))
    assert legacy.calls.count('GetItem') == downloads + 1
    assert second['revenue.csv']['indexed'] is True and 'north' in second['revenue.csv']['keywords']


def test_rejected_login_during_content_indexing_fails_the_run(legacy):
    c = client(drive_name='Documents', recursive=True, index_mode='content')
    legacy.override = lambda op, command: (b'', 401) if op == 'GetItem' else None
    with pytest.raises(SharePointHTTPError):
        c.get_schemas()
