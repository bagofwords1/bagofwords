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

import requests
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException

from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
from app.data_sources.clients.progress import make_reporter
from app.data_sources.clients.sharepoint_onprem_client import SharePointHTTPError, _safe_path

SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
SP = 'http://schemas.microsoft.com/sharepoint/soap/'
RS = 'urn:schemas-microsoft-com:rowset'
FIELDS = ('ID', 'FileRef', 'FileLeafRef', 'FSObjType', 'File_x0020_Size', 'Modified')


def _value(row, field):
    # Document lookups use ID;#value. Do not split arbitrary text or filenames.
    return re.sub(r'^\d+;#', '', row.get('ows_' + field, ''), count=1)


def _text(node, name, value):
    ET.SubElement(node, f"{{{SP}}}{name}").text = value


class Sharepoint2010API:
    page_size = 200  # The read-only smoke tool can lower this to exercise paging.

    def __init__(self, client):
        self.client = client

    def _soap(self, operation, fill=None, *, byte_limit=8 * 1024 * 1024):
        service = {'GetListCollection': 'Lists', 'GetList': 'Lists',
                   'GetListItems': 'Lists', 'GetItem': 'Copy'}[operation]
        envelope = ET.Element(f'{{{SOAP}}}Envelope')
        command = ET.SubElement(ET.SubElement(envelope, f'{{{SOAP}}}Body'), f'{{{SP}}}{operation}')
        if fill:
            fill(command)
        payload = ET.tostring(envelope, encoding='utf-8', xml_declaration=True)
        c = self.client
        for attempt in range(2):
            try:
                with c._session().post(c.site_url + f'/_vti_bin/{service}.asmx', data=payload,
                        headers={'Content-Type': 'text/xml; charset=utf-8', 'Accept': 'text/xml',
                                 'SOAPAction': f'"{SP}{operation}"'},
                        timeout=(10, 45), allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise ValueError('SharePoint redirected the request. Use the configured public site URL.')
                    if status in (401, 403):
                        raise ValueError('SharePoint rejected access. Check the account authentication and resource permissions.')
                    if status not in (200, 500):
                        raise SharePointHTTPError(status)
                    if int(response.headers.get('Content-Length', '0')) > byte_limit:
                        raise FileTooLargeError('SharePoint SOAP response exceeds the configured byte limit.')
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > byte_limit:
                            raise FileTooLargeError('SharePoint SOAP response exceeds the configured byte limit.')
                        chunks.append(chunk)
                    try:
                        root = fromstring(b''.join(chunks), forbid_dtd=True)
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
            except requests.exceptions.SSLError as exc:
                raise ValueError('SharePoint TLS verification failed. Install the issuing CA in the backend trust store.') from exc
            except requests.RequestException as exc:
                if hasattr(c._local, 'session'):
                    c._local.session.close()
                    del c._local.session
                if attempt:
                    raise ValueError('Cannot reach SharePoint. Check DNS, network access, and server availability.') from exc

    def _get_list(self, name):
        result = self._soap('GetList', lambda node: _text(node, 'listName', name))
        lib = result.find(f'{{{SP}}}GetListResult/{{{SP}}}List')
        if lib is None:
            raise ValueError('SharePoint returned no library metadata.')
        return dict(lib.attrib)

    def _libraries(self):
        c = self.client
        if c.drive_name == '*':
            result = self._soap('GetListCollection')
            container = result.find(f'{{{SP}}}GetListCollectionResult/{{{SP}}}Lists')
            if container is None:
                raise ValueError('SharePoint returned no library collection.')
            libs = [dict(node.attrib) for node in container.findall(f'{{{SP}}}List')]
            libs = [lib for lib in libs if lib.get('ServerTemplate') == '101' and lib.get('Hidden', '').lower() == 'false']
        else:
            libs = [self._get_list(c.drive_name)]
        chosen = []
        for lib in libs:
            if lib.get('ServerTemplate') != '101' or lib.get('Hidden', '').lower() != 'false':
                raise ValueError('The selected list is not a visible document library.')
            if not lib.get('RootFolder'):
                lib = self._get_list(lib['ID'])
            root = _safe_path(lib.get('RootFolder', '').rstrip('/'))
            if not root.startswith('/') or not root.casefold().startswith((c._site_path + '/').casefold()):
                raise GlobScopeError('Library is outside the configured site.')
            if not lib.get('ID') or not lib.get('Title'):
                raise ValueError('SharePoint returned incomplete library metadata.')
            lib = {'id': lib['ID'], 'title': lib['Title'], 'root': root, 'scope': root}
            if c.folder_path:
                scope = root + '/' + c.folder_path
                try:
                    self._find(lib, scope, folder=True)
                except SharePointHTTPError as exc:
                    if c.drive_name == '*' and exc.status == 404:
                        continue
                    raise
                lib['scope'] = scope
            chosen.append(lib)
        if not chosen:
            raise ValueError('No matching document library or folder is accessible on this site.')
        return chosen

    @staticmethod
    def _scopes(libraries):
        return [(lib['title'], lib['scope']) for lib in libraries]

    def libraries(self):
        return self._scopes(self._libraries())

    def _items(self, lib, folder):
        token, seen = None, set()
        for _ in range(1000):
            def fill(command):
                ET.SubElement(command, f'{{{SP}}}listName').text = lib['id']
                query = ET.SubElement(ET.SubElement(command, f'{{{SP}}}query'), 'Query')
                ET.SubElement(ET.SubElement(query, 'OrderBy'), 'FieldRef', Name='ID', Ascending='TRUE')
                fields = ET.SubElement(ET.SubElement(command, f'{{{SP}}}viewFields'), 'ViewFields')
                for field in FIELDS:
                    ET.SubElement(fields, 'FieldRef', Name=field)
                ET.SubElement(command, f'{{{SP}}}rowLimit').text = str(self.page_size)
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
                length = _value(row, 'File_x0020_Size')
                if kind == '0' and (not length.isdigit()):
                    raise ValueError('SharePoint returned no valid file size.')
                yield {'Name': name, 'ServerRelativeUrl': path, 'Length': int(length or 0),
                       'TimeLastModified': row.get('ows_Modified'), 'folder': kind == '1'}
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
        parts = path[len(root) + 1:].split('/')
        current, found = root, None
        for index, part in enumerate(parts):
            found = next((item for item in self._items(lib, current) if item['Name'].casefold() == part.casefold()), None)
            if found is None or ((index < len(parts) - 1 or folder) != found['folder']):
                raise SharePointHTTPError(404)
            current = found['ServerRelativeUrl']
        return found

    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None):
        c = self.client
        cap = min(max(0, int(limit)) if limit is not None else c.max_catalog_objects, c.max_catalog_objects)
        if not cap:
            return []
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
        files, seen, ids = [], set(), set()
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
            for meta in self._items(lib, folder):
                if meta['folder']:
                    if recurse and meta['Name'] != 'Forms':
                        queue.append((lib, meta['ServerRelativeUrl']))
                    continue
                try:
                    entry = c._entry(meta, scopes)
                except GlobScopeError:
                    continue
                if entry['id'].casefold() not in ids:
                    files.append(entry)
                    ids.add(entry['id'].casefold())
                if len(files) >= cap:
                    return files
        return files

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
