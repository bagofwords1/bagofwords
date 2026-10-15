"""Read-only SharePoint 2010 SOAP adapter (MS-LISTSWS and MS-COPYS).

SharePlum supplies useful SOAP patterns, but its Folder API is modern REST.
Only these fixed read operations are sent; XML is parsed without DTD/entities.
"""
from __future__ import annotations

import base64
import binascii
import mimetypes
import re
from collections import deque
from urllib.parse import quote
from xml.etree import ElementTree as ET

from threading import Lock
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException

from app.data_sources.clients._file_source_common import INDEX_CONTENT, FileTooLargeError, GlobScopeError
from app.data_sources.clients._sharepoint_api import SharepointAPI
from app.data_sources.clients.progress import make_reporter
from app.data_sources.clients.sharepoint_onprem_client import SharePointHTTPError, _safe_path

SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
SP = 'http://schemas.microsoft.com/sharepoint/soap/'
RS = 'urn:schemas-microsoft-com:rowset'
FIELDS = ('ID', 'FileRef', 'FileLeafRef', 'FSObjType', 'File_x0020_Size', 'Modified', 'Created')


def _value(row, field):
    # Document lookups use ID;#value. Do not split arbitrary text or filenames.
    return re.sub(r'^\d+;#', '', row.get('ows_' + field, ''), count=1)


def _text(node, name, value):
    ET.SubElement(node, f"{{{SP}}}{name}").text = value


class Sharepoint2010API(SharepointAPI):
    page_size = 200  # The read-only smoke tool can lower this to exercise paging.

    def __init__(self, client):
        super().__init__(client)
        self._metadata = None
        self._metadata_lock = Lock()

    def _soap(self, operation, fill=None, *, byte_limit=8 * 1024 * 1024):
        service = {'GetListCollection': 'Lists', 'GetList': 'Lists',
                   'GetListItems': 'Lists', 'GetItem': 'Copy'}[operation]
        envelope = ET.Element(f'{{{SOAP}}}Envelope')
        command = ET.SubElement(ET.SubElement(envelope, f'{{{SOAP}}}Body'), f'{{{SP}}}{operation}')
        if fill:
            fill(command)
        payload = ET.tostring(envelope, encoding='utf-8', xml_declaration=True)
        c = self.client
        status, content = c._request_bytes('post', c.site_url + f'/_vti_bin/{service}.asmx',
            data=payload, headers={'Content-Type': 'text/xml; charset=utf-8', 'Accept': 'text/xml',
                                   'SOAPAction': f'"{SP}{operation}"'},
            byte_limit=byte_limit, file_content=operation == 'GetItem', accepted_statuses=(200, 500))
        try:
            root = fromstring(content, forbid_dtd=True)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise ValueError('SharePoint returned invalid SOAP XML.') from exc
        body = root.find(f'{{{SOAP}}}Body')
        if root.tag != f'{{{SOAP}}}Envelope' or body is None:
            raise ValueError('SharePoint returned an invalid SOAP envelope.')
        fault = body.find(f'{{{SOAP}}}Fault')
        if fault is not None:
            code = next((node.text for node in fault.iter() if node.tag == f'{{{SP}}}errorcode'), '')
            if code == '0x82000006':
                raise SharePointHTTPError(404)
            # Provider messages can contain sensitive server paths or data.
            raise ValueError(f'SharePoint SOAP {operation} failed. Check resource permissions and server query limits.')
        if status != 200:
            raise SharePointHTTPError(status)
        result = body.find(f'{{{SP}}}{operation}Response')
        if result is None:
            raise ValueError(f'SharePoint returned no {operation} response.')
        return result

    def _get_list(self, name):
        result = self._soap('GetList', lambda node: _text(node, 'listName', name))
        lib = result.find(f'{{{SP}}}GetListResult/{{{SP}}}List')
        if lib is None:
            raise ValueError('SharePoint returned no library metadata.')
        return dict(lib.attrib)

    def _load_library_metadata(self):
        c = self.client
        if c.drive_name == '*':
            result = self._soap('GetListCollection')
            container = result.find(f'{{{SP}}}GetListCollectionResult/{{{SP}}}Lists')
            if container is None:
                raise ValueError('SharePoint returned no library collection.')
            libs = [dict(node.attrib) for node in container.findall(f'{{{SP}}}List')]
        else:
            libs = [self._get_list(c.drive_name)]
        chosen = []
        for lib in libs:
            # BaseType identifies the document-library family, including custom templates.
            if lib.get('BaseType') != '1' or lib.get('Hidden', '').lower() != 'false':
                if c.drive_name == '*':
                    continue
                raise ValueError('The selected list is not a visible document library.')
            if not lib.get('ID') or not lib.get('Title'):
                raise ValueError('SharePoint returned incomplete library metadata.')
            if not lib.get('RootFolder'):
                lib = self._get_list(lib['ID'])
                if not lib.get('ID') or not lib.get('Title') or not lib.get('RootFolder'):
                    raise ValueError('SharePoint returned incomplete library metadata.')
                if lib.get('BaseType') != '1' or lib.get('Hidden', '').lower() != 'false':
                    raise ValueError('The selected list is not a visible document library.')
            root = _safe_path(lib['RootFolder'].rstrip('/'))
            if not root.startswith('/') or not root.casefold().startswith((c._site_path + '/').casefold()):
                raise GlobScopeError('Library is outside the configured site.')
            chosen.append({'id': lib['ID'], 'title': lib['Title'], 'root': root, 'scope': root})
        if not chosen:
            raise ValueError('No matching document library is accessible on this site.')
        return chosen

    def _libraries(self):
        # Cache discovery only on this client/identity. Never cache file rows,
        # folder existence, permissions, or bytes. Failed discovery is retriable.
        with self._metadata_lock:
            if self._metadata is None:
                self._metadata = self._load_library_metadata()
            libraries = [dict(lib) for lib in self._metadata]
        c = self.client
        chosen = []
        for lib in libraries:
            if c.folder_path:
                scope = lib['root'] + '/' + c.folder_path
                try:
                    meta = self._find(lib, scope, folder=True)
                except SharePointHTTPError as exc:
                    if c.drive_name == '*' and exc.status == 404:
                        continue
                    raise
                lib['scope'] = meta['ServerRelativeUrl']
            chosen.append(lib)
        if not chosen:
            raise ValueError('No matching document library or folder is accessible on this site.')
        return chosen

    @staticmethod
    def _scopes(libraries):
        return [(lib['title'], lib['scope']) for lib in libraries]

    def _items(self, lib, folder, *, leaf=None, page_size=None):
        token, seen = None, set()
        for _ in range(1000):
            def fill(command):
                ET.SubElement(command, f'{{{SP}}}listName').text = lib['id']
                query = ET.SubElement(ET.SubElement(command, f'{{{SP}}}query'), 'Query')
                if leaf is not None:
                    eq = ET.SubElement(ET.SubElement(query, 'Where'), 'Eq')
                    ET.SubElement(eq, 'FieldRef', Name='FileLeafRef')
                    ET.SubElement(eq, 'Value', Type='Text').text = leaf
                ET.SubElement(ET.SubElement(query, 'OrderBy'), 'FieldRef', Name='ID', Ascending='TRUE')
                fields = ET.SubElement(ET.SubElement(command, f'{{{SP}}}viewFields'), 'ViewFields')
                for field in FIELDS:
                    ET.SubElement(fields, 'FieldRef', Name=field)
                ET.SubElement(command, f'{{{SP}}}rowLimit').text = str(1 if leaf is not None else (page_size or self.page_size))
                options = ET.SubElement(ET.SubElement(command, f'{{{SP}}}queryOptions'), 'QueryOptions')
                ET.SubElement(options, 'Folder').text = folder
                ET.SubElement(options, 'ViewAttributes', Scope='')  # immediate files and folders
                ET.SubElement(options, 'DateInUtc').text = 'True'
                if token:
                    ET.SubElement(options, 'Paging', ListItemCollectionPositionNext=token)
            result = self._soap('GetListItems', fill)
            rows = result.find(f'{{{SP}}}GetListItemsResult/{{{SP}}}listitems/{{{RS}}}data')
            if rows is None:
                raise ValueError('SharePoint returned no file rows.')
            for row in rows.findall('{#RowsetSchema}row'):
                path = _value(row, 'FileRef')
                name = _value(row, 'FileLeafRef')
                kind = _value(row, 'FSObjType')
                if not path or not name or kind not in ('0', '1'):
                    raise ValueError('SharePoint returned incomplete file metadata.')
                path = _safe_path('/' + path.lstrip('/'))
                if path.rsplit('/', 1)[0].casefold() != folder.casefold() or path.rsplit('/', 1)[1] != name:
                    raise GlobScopeError('SharePoint returned an item outside the requested folder.')
                if leaf is not None and name.casefold() != leaf.casefold():
                    raise GlobScopeError('SharePoint returned an item outside the requested path.')
                length = _value(row, 'File_x0020_Size')
                if kind == '0' and (not length.isdigit()):
                    raise ValueError('SharePoint returned no valid file size.')
                yield {'Name': name, 'ServerRelativeUrl': path, 'Length': int(length or 0),
                       'TimeLastModified': row.get('ows_Modified'), 'TimeCreated': row.get('ows_Created'),
                       'folder': kind == '1'}
            token = rows.get('ListItemCollectionPositionNext')
            if not token:
                return
            if token in seen:
                raise ValueError('SharePoint returned a pagination cycle.')
            seen.add(token)
        raise ValueError('SharePoint pagination limit exceeded; narrow the folder scope.')

    def _find(self, lib, path, *, folder=False):
        root = lib['root']
        if folder and path.casefold() == root.casefold():
            return {'ServerRelativeUrl': root, 'Name': root.rsplit('/', 1)[1], 'folder': True}
        if not path.casefold().startswith((root + '/').casefold()):
            raise GlobScopeError('Path is outside the document library.')
        # A folder-scoped FileLeafRef equality query is documented in CAML.
        # It resolves a deep path in one request, without enumerating ancestors.
        parent, leaf = path.rsplit('/', 1)
        found = next(self._items(lib, parent, leaf=leaf), None)
        if found is None or found['folder'] != folder:
            raise SharePointHTTPError(404)
        return found

    def iter_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None, *, page_size=None):
        c = self.client
        cap = min(max(0, int(limit)) if limit is not None else c.max_catalog_objects, c.max_catalog_objects)
        if not cap:
            return
        libraries = self._libraries()
        scopes = self._scopes(libraries)
        queue = deque()
        if folder_id:
            c._scope(folder_id, scopes, folder=True)
            lib = next(lib for lib in libraries if folder_id.casefold() == lib['scope'].casefold() or folder_id.casefold().startswith((lib['scope'] + '/').casefold()))
            queue.append((lib, self._find(lib, folder_id, folder=True)['ServerRelativeUrl']))
        else:
            queue.extend((lib, lib['scope']) for lib in libraries)
        recurse = c.recursive if recursive is None else recursive
        # Folder cycles and repeated file rows are different deduplication domains.
        seen, ids = set(), set()
        reporter = make_reporter(progress_callback) if progress_callback else None
        if reporter:
            reporter.phase('listing SharePoint libraries', total=len(queue))
        while queue:
            lib, folder = queue.popleft()
            if folder.casefold() in seen:
                continue
            seen.add(folder.casefold())
            if len(seen) > c.max_catalog_objects:
                raise ValueError('Folder traversal limit exceeded; narrow the folder scope.')
            if reporter:
                reporter.set_total(len(seen) + len(queue))
                reporter.item(folder, done=len(seen) - 1)
            for meta in self._items(lib, folder, page_size=page_size):
                if meta['folder']:
                    if recurse and meta['Name'] != 'Forms':
                        queue.append((lib, meta['ServerRelativeUrl']))
                    continue
                try:
                    entry = c._entry(meta, scopes)
                except GlobScopeError:
                    continue
                if entry['id'].casefold() not in ids:
                    ids.add(entry['id'].casefold())
                    yield entry
                if len(ids) >= cap:
                    return

    def read_raw_bytes(self, file_id, *, max_bytes=None):
        c = self.client
        libraries = self._libraries()
        scopes = self._scopes(libraries)
        value = _safe_path(str(file_id))
        if value.startswith('/'):
            c._scope(value, scopes)
        candidates = []
        scope_error = None
        qualified = [lib for lib in libraries if c.drive_name == '*' and value.startswith(lib['title'] + '/')]
        for lib in qualified or libraries:
            rel = value[len(lib['title']) + 1:] if lib in qualified else value
            path = rel if rel.startswith('/') else lib['scope'] + '/' + rel
            try:
                c._scope(path, [(lib['title'], lib['scope'])])
                meta = self._find(lib, path)
                c._scope(meta['ServerRelativeUrl'], scopes)
                candidates.append(meta)
            except GlobScopeError as exc:
                scope_error = exc
            except SharePointHTTPError as exc:
                if exc.status != 404:
                    raise
        if not candidates and scope_error:
            raise scope_error
        if len(candidates) != 1:
            raise ValueError('File path is missing or ambiguous. Use the id returned by list_files/search_files.')
        meta = candidates[0]
        limit = min(c._max_bytes, max_bytes) if max_bytes is not None else c._max_bytes
        if limit < 1 or meta['Length'] > limit:
            raise FileTooLargeError('File exceeds the configured byte limit; increase it explicitly to read this file.')
        url = c._origin_url + quote(meta['ServerRelativeUrl'], safe='/')
        # Base64 expansion plus a bounded allowance for XML/metadata.
        result = self._soap('GetItem', lambda node: _text(node, 'Url', url),
                            byte_limit=4 * ((int(limit) + 2) // 3) + 1024 * 1024)
        stream = result.find(f'{{{SP}}}Stream')
        if stream is None:
            raise SharePointHTTPError(404)
        try:
            content = base64.b64decode(''.join((stream.text or '').split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('SharePoint returned invalid file content.') from exc
        if len(content) > limit:
            raise FileTooLargeError('File exceeds the configured byte limit; increase it explicitly to read this file.')
        return content, meta['Name'], mimetypes.guess_type(meta['Name'])[0]

    @property
    def description(self):
        c = self.client
        search = ("search_files matches filenames, paths and indexed document keywords "
                  "from the content index; a live fallback matches filenames and paths only."
                  if c.index_mode == INDEX_CONTENT else
                  "search_files matches filenames and paths within the configured enumeration "
                  "limit; it does not search document contents.")
        return ("Read-only SharePoint 2010 document libraries. Use list_files and read_file "
                f"with returned file ids or scoped paths. {search} "
                "CSV/Excel reads return DataFrames; PDF/DOCX/PPTX return document text.")

    def search_files(self, query, limit=100, **_):
        if not str(query).strip():
            return []
        cap = min(max(0, int(limit)), self.client.max_catalog_objects, 500)
        if not cap:
            return []
        found = []
        for entry in self.iter_files():
            if str(query).casefold() in entry['path'].casefold():
                found.append(entry)
                if len(found) >= cap:
                    break
        return found

    def test_connection(self):
        c = self.client
        try:
            self.list_files(limit=1, recursive=False)
            return {"success": True, "message": "Connected to SharePoint 2010.",
                    "details": {"site": c.site_url, "api_mode": c.api_mode,
                                "auth_method": "kerberos" if c.kerberos else "ntlm"}}
        except Exception as exc:
            return {"success": False, "message": str(exc)}
