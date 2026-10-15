"""SharePoint Server REST adapter; authentication and transport live on the client."""
import json
import mimetypes
from collections import deque
from urllib.parse import unquote, urlsplit

from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
from app.data_sources.clients._sharepoint_api import SharepointAPI
from app.data_sources.clients.sharepoint_onprem_client import SharePointHTTPError, _literal, _safe_path, unresolved_path_error
from app.data_sources.clients.progress import make_reporter


class SharepointRestAPI(SharepointAPI):
    @property
    def description(self):
        return (
            "Read-only SharePoint Server document libraries. Use list_files, search_files, "
            "and read_file with the returned server-relative file id or scoped path. "
            "Search uses the SharePoint content index and live filename matches; newly "
            "uploaded document contents require a SharePoint search crawl. CSV/Excel "
            "reads return DataFrames. PDF/DOCX/PPTX reads return document text."
        )

    def _request(self, path, *, params=None, binary=False, byte_limit=None):
        c = self.client
        url = path if path.startswith(("http://", "https://")) else c.site_url + "/_api/" + path
        p = urlsplit(url)
        if ((p.scheme.lower(), p.netloc.lower()) != c._origin
                or not unquote(p.path).startswith(c._site_path + "/_api/")
                or p.username or p.password or p.fragment):
            raise ValueError("Refusing a SharePoint API link outside the configured site.")
        _safe_path(unquote(p.path))
        limit = min(byte_limit if byte_limit is not None else c._max_bytes, c._max_bytes) if binary else 8 * 1024 * 1024
        _, content = c._request_bytes("get", url, params=params, byte_limit=limit, file_content=binary)
        if binary:
            return content
        data = json.loads(content)
        return data.get("d", data)

    def _pages(self, path, params=None):
        visited = set()
        for _ in range(1000):
            if path in visited:
                raise ValueError("SharePoint returned a pagination cycle.")
            visited.add(path)
            data = self._request(path, params=params)
            for row in data.get("value", data.get("results", [])):
                yield row
            path = data.get("odata.nextLink") or data.get("@odata.nextLink") or data.get("__next")
            params = None
            if not path:
                return
        raise ValueError("SharePoint pagination limit exceeded; narrow the connection scope.")

    def libraries(self):
        libs = list(self._pages("web/lists", {
            "$filter": "BaseType eq 1 and Hidden eq false",
            "$select": "Id,Title,RootFolder/ServerRelativeUrl", "$expand": "RootFolder", "$top": 200,
        }))
        if self.client.drive_name == "*":
            chosen = libs
        elif self.client.drive_name:
            chosen = [lib for lib in libs if lib["Title"].casefold() == self.client.drive_name.casefold()]
        else:
            default = self._request("web/DefaultDocumentLibrary", params={"$select": "Id"})
            chosen = [lib for lib in libs if lib["Id"] == default["Id"]]
        if not chosen:
            raise ValueError("No matching document library is accessible on this site.")
        result = []
        for lib in chosen:
            root = _safe_path(lib["RootFolder"]["ServerRelativeUrl"].rstrip("/"))
            if not root.casefold().startswith((self.client._site_path + "/").casefold()):
                raise GlobScopeError("Library is outside the configured site.")
            result.append((lib["Title"], root + ("/" + self.client.folder_path if self.client.folder_path else "")))
        return result

    def iter_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None, *, page_size=None):
        cap = min(max(0, int(limit)) if limit is not None else self.client.max_catalog_objects, self.client.max_catalog_objects)
        if cap == 0:
            return
        libraries = self.libraries()
        roots = [folder_id] if folder_id else [root for _, root in libraries]
        if folder_id:
            self.client._scope(folder_id, libraries, folder=True)
        queue, seen, ids = deque(roots), set(), set()
        recurse = self.client.recursive if recursive is None else recursive
        reporter = make_reporter(progress_callback) if progress_callback else None
        if reporter:
            reporter.phase("listing SharePoint libraries", total=len(roots))
        while queue:
            root = queue.popleft()
            self.client._scope(root, libraries, folder=True)
            if root.casefold() in seen:
                continue
            seen.add(root.casefold())
            if reporter:
                reporter.set_total(len(seen) + len(queue))
                reporter.item(root, done=len(seen) - 1)
            if len(seen) > self.client.max_catalog_objects:
                raise ValueError("Folder traversal limit exceeded; narrow the folder scope.")
            endpoint = f"web/GetFolderByServerRelativePath(decodedurl={_literal(root)})"
            try:
                for meta in self._pages(endpoint + "/Files", {"$select": "Name,ServerRelativeUrl,Length,TimeLastModified,TimeCreated", "$top": min(cap, page_size or 200)}):
                    try:
                        entry = self.client._entry(meta, libraries)
                    except GlobScopeError:
                        continue
                    if entry['id'].casefold() in ids:
                        continue
                    ids.add(entry['id'].casefold())
                    yield entry
                    if len(ids) >= cap:
                        return
                if recurse:
                    for child in self._pages(endpoint + "/Folders", {"$select": "Name,ServerRelativeUrl", "$top": 200}):
                        if child["Name"] != "Forms":
                            queue.append(child["ServerRelativeUrl"])
            except SharePointHTTPError as exc:
                # Missing scoped folder across * libraries is normal; access
                # failures are NOT silently turned into an empty catalog.
                if exc.status == 404 and root in roots and self.client.drive_name == "*" and self.client.folder_path:
                    continue
                raise

    def _resolve(self, file_id, libraries):
        value = str(file_id)
        if value.startswith("/"):
            self.client._scope(value, libraries)
            return value
        _safe_path(value)
        candidates = []
        qualified = [(name, root) for name, root in libraries
                     if self.client.drive_name == "*" and value.startswith(name + "/")]
        scope_error = None
        for name, root in qualified or libraries:
            rel = value
            if self.client.drive_name == "*" and value.startswith(name + "/"):
                rel = value[len(name) + 1:]
            candidate = root + "/" + rel
            try:
                self.client._scope(candidate, libraries)
                meta = self._request(f"web/GetFileByServerRelativePath(decodedurl={_literal(candidate)})", params={"$select": "ServerRelativeUrl"})
                self.client._scope(meta["ServerRelativeUrl"], libraries)
                candidates.append(meta["ServerRelativeUrl"])
            except GlobScopeError as exc:
                scope_error = exc
            except SharePointHTTPError as exc:
                if exc.status != 404:
                    raise
        if not candidates and scope_error:
            raise scope_error
        if len(candidates) != 1:
            raise unresolved_path_error(value, candidates)
        return candidates[0]

    def read_raw_bytes(self, file_id, *, max_bytes=None):
        libraries = self.libraries()
        path = self._resolve(file_id, libraries)
        endpoint = f"web/GetFileByServerRelativePath(decodedurl={_literal(path)})"
        meta = self._request(endpoint, params={"$select": "Name,ServerRelativeUrl,Length,TimeLastModified,TimeCreated"})
        self.client._scope(meta["ServerRelativeUrl"], libraries)
        limit = min(self.client._max_bytes, max_bytes) if max_bytes is not None else self.client._max_bytes
        if limit < 1 or int(meta.get("Length") or 0) > limit:
            raise FileTooLargeError("File exceeds the configured byte limit; increase it explicitly to read this file.")
        content = self._request(endpoint + "/$value", binary=True, byte_limit=limit)
        return content, meta["Name"], mimetypes.guess_type(meta["Name"])[0]

    def search_files(self, query, limit=100, **_):
        if not str(query).strip():
            return []
        cap = min(max(0, int(limit)), self.client.max_catalog_objects, 500)
        if not cap:
            return []
        libraries = self.libraries()
        # Quoted literal query: do not let model-generated KQL escape scope.
        term = str(query).replace('"', ' ').replace("\\", " ").strip()
        kql = f'"{term}" AND IsDocument:True AND Path:"{self.client.site_url}/"'
        data = self._request("search/query", params={
            "querytext": "'" + kql.replace("'", "''") + "'",
            "rowlimit": cap, "trimduplicates": "false", "selectproperties": "'Path,Title'",
        })
        table = data.get("PrimaryQueryResult", {}).get("RelevantResults", {}).get("Table", {})
        rows = table.get("Rows", [])
        rows = rows.get("results", []) if isinstance(rows, dict) else rows
        found = {}
        for row in rows:
            cells = row.get("Cells", [])
            cells = cells.get("results", []) if isinstance(cells, dict) else cells
            values = {cell["Key"]: cell.get("Value") for cell in cells}
            url = urlsplit(values.get("Path") or "")
            if (url.scheme.lower(), url.netloc.lower()) != self.client._origin:
                continue
            path = unquote(url.path)
            try:
                self.client._scope(path, libraries)
                meta = self._request(f"web/GetFileByServerRelativePath(decodedurl={_literal(path)})", params={"$select": "Name,ServerRelativeUrl,Length,TimeLastModified,TimeCreated"})
                entry = self.client._entry(meta, libraries)
                found[entry["id"]] = entry
            except GlobScopeError:
                continue
            except SharePointHTTPError as exc:
                if exc.status not in (403, 404):
                    raise
        # Indexing is asynchronous on Server: supplement indexed content hits
        # with live filename matches (never pretend to search uncrawled content).
        if len(found) >= cap:
            return list(found.values())[:cap]
        for entry in self.iter_files():
            if str(query).casefold() in entry["path"].casefold():
                found[entry["id"]] = entry
            if len(found) >= cap:
                break
        return list(found.values())[:cap]

    def test_connection(self):
        try:
            web = self._request("web", params={"$select": "Title,Url"})
            user = self._request("web/currentuser", params={"$select": "LoginName"})
            self.list_files(limit=1, recursive=False)
            return {"success": True, "message": "Connected to SharePoint Server.",
                    "details": {"site": web.get("Title"), "identity": user.get("LoginName"),
                                "auth_method": "kerberos" if self.client.kerberos else "ntlm"}}
        except Exception as exc:
            return {"success": False, "message": str(exc)}
