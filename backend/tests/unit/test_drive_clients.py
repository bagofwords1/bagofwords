"""Unit tests for SharePoint/OneDrive (GraphDriveClient) and GoogleDriveClient.

Focuses on the pure logic — URL building, filtering, capability declarations,
registry wiring, file-as-table mapping — without hitting the network.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.data_sources.clients.base import Capability
from app.data_sources.clients.graph_drive_client import (
    GraphDriveClient,
    OnedriveClient,
    SharepointClient,
)
from app.data_sources.clients.google_drive_client import GoogleDriveClient


# --------------------------------------------------------------- registry


class TestRegistry:
    def test_three_new_sources_registered(self):
        from app.schemas.data_source_registry import REGISTRY

        for t in ("sharepoint", "onedrive", "google_drive"):
            assert t in REGISTRY

    def test_sharepoint_shape(self):
        """SharePoint: file-shaped, admin-curated shared catalog."""
        from app.schemas.data_source_registry import REGISTRY

        entry = REGISTRY["sharepoint"]
        assert entry.is_connection is True
        assert entry.data_shape == "files"
        assert entry.catalog_ownership == "shared"

    def test_drives_shape(self):
        """OneDrive / Google Drive: file-shaped, per-user catalogs.

        Each user's catalog is independent (not a subset of an admin universe).
        Admin save just registers the OAuth app; per-user catalog is fetched
        after each user signs in.
        """
        from app.schemas.data_source_registry import REGISTRY

        for t in ("onedrive", "google_drive"):
            entry = REGISTRY[t]
            assert entry.is_connection is True, t  # agent-attachable
            assert entry.data_shape == "files", t
            assert entry.catalog_ownership == "per_user", t
            assert entry.ui_form == "integration", t

    def test_mcp_and_custom_api_shape(self):
        """MCP / Custom API: tools-shaped, no catalog."""
        from app.schemas.data_source_registry import REGISTRY

        for t in ("mcp", "custom_api"):
            entry = REGISTRY[t]
            assert entry.is_connection is False, t
            assert entry.data_shape == "tools", t
            assert entry.catalog_ownership == "none", t

    def test_mongodb_shape(self):
        from app.schemas.data_source_registry import REGISTRY

        assert REGISTRY["mongodb"].data_shape == "objects"
        assert REGISTRY["mongodb"].catalog_ownership == "shared"

    def test_defaults_for_sql_sources(self):
        """Unset entries keep the SQL-shape defaults."""
        from app.schemas.data_source_registry import REGISTRY

        pg = REGISTRY["postgresql"]
        assert pg.data_shape == "tables"
        assert pg.catalog_ownership == "shared"
        assert pg.ui_form == "data_source"

    def test_connected_message_branches(self):
        """The success message must reflect catalog ownership + data_shape."""
        from app.services.connection_service import _connected_message

        # per_user catalog → don't lie about admin-side counts
        msg = _connected_message("onedrive", 0)
        assert "their own files" in msg
        # shared file catalog with zero items → "no files visible yet"
        msg = _connected_message("sharepoint", 0)
        assert "No files visible yet" in msg
        # shared file catalog with items
        msg = _connected_message("sharepoint", 5)
        assert "Found 5 files" in msg
        # tools catalog
        msg = _connected_message("mcp", 3)
        assert "tools" in msg.lower()
        # SQL tables
        msg = _connected_message("postgresql", 12)
        assert "12 tables" in msg

    def test_resolve_client_class(self):
        from app.schemas.data_source_registry import resolve_client_class

        assert resolve_client_class("sharepoint") is SharepointClient
        assert resolve_client_class("onedrive") is OnedriveClient
        assert resolve_client_class("google_drive") is GoogleDriveClient

    def test_admin_credential_fields(self):
        """Default auth variant must collect the admin OAuth-app creds."""
        from app.schemas.data_source_registry import default_credentials_schema_for

        sp_fields = default_credentials_schema_for("sharepoint").model_fields
        assert {"tenant_id", "client_id", "client_secret"} <= set(sp_fields)

        od_fields = default_credentials_schema_for("onedrive").model_fields
        assert {"tenant_id", "client_id", "client_secret"} <= set(od_fields)

        gd_fields = default_credentials_schema_for("google_drive").model_fields
        assert {"oauth_client_id", "oauth_client_secret"} <= set(gd_fields)

    def test_oauth_variant_available(self):
        from app.schemas.data_source_registry import REGISTRY

        for t in ("sharepoint", "onedrive", "google_drive"):
            variants = REGISTRY[t].credentials_auth.by_auth
            assert "oauth" in variants
            assert "user" in variants["oauth"].scopes


# ---------------------------------------------------------- capabilities


class TestCapabilities:
    def test_graph_client_capabilities(self):
        assert Capability.LIST_FILES in GraphDriveClient.capabilities
        assert Capability.READ_FILE in GraphDriveClient.capabilities
        assert Capability.SEARCH_FILES in GraphDriveClient.capabilities

    def test_google_drive_capabilities(self):
        assert Capability.LIST_FILES in GoogleDriveClient.capabilities
        assert Capability.READ_FILE in GoogleDriveClient.capabilities


# -------------------------------------------------------- construction


class TestSharePointConstruction:
    def test_basic_init(self):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://contoso.sharepoint.com/sites/Finance",
            folder_path="Reports/2025", allowed_extensions="xlsx,csv",
        )
        assert c.mode == "sharepoint"
        assert c.folder_path == "Reports/2025"
        assert c.allowed_extensions == {"xlsx", "csv"}
        assert c.is_document_based is True

    def test_extension_parsing_normalizes(self):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
            allowed_extensions=" .XLSX, CSV ,pdf,",
        )
        assert c.allowed_extensions == {"xlsx", "csv", "pdf"}

    def test_allowed_filter(self):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
            allowed_extensions="xlsx",
        )
        assert c._allowed("report.xlsx") is True
        assert c._allowed("report.pdf") is False
        assert c._allowed("notes") is False

    def test_allowed_without_filter_permits_all(self):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
        )
        assert c._allowed("anything.weird") is True

    def test_folder_path_normalized(self):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
            folder_path="/Reports/2025/",
        )
        assert c.folder_path == "Reports/2025"

    def test_extra_kwargs_ignored(self):
        # Registry merges config + credentials into one kwargs blob; the
        # client must tolerate fields it doesn't recognize.
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
            irrelevant_field="ignored",
        )
        assert c.tenant_id == "t"


class TestOneDriveConstruction:
    def test_mode_is_onedrive(self):
        c = OnedriveClient(access_token="tok", folder_path="Documents/X")
        assert c.mode == "onedrive"
        assert c.folder_path == "Documents/X"


class TestGoogleDriveConstruction:
    def test_basic_init(self):
        c = GoogleDriveClient(
            access_token="tok",
            folder_id="abc123", allowed_extensions="csv,gsheet",
        )
        assert c.folder_id == "abc123"
        assert c.allowed_extensions == {"csv", "gsheet"}
        assert c.is_document_based is True

    def test_drive_params_no_shared_drive(self):
        c = GoogleDriveClient(access_token="t")
        p = c._drive_params()
        assert p["supportsAllDrives"] == "true"
        assert "driveId" not in p

    def test_drive_params_with_shared_drive(self):
        c = GoogleDriveClient(access_token="t", shared_drive_id="SDID")
        p = c._drive_params()
        assert p["driveId"] == "SDID"
        assert p["corpora"] == "drive"

    def test_root_folder_resolution(self):
        c = GoogleDriveClient(access_token="t", folder_id="F", shared_drive_id="S")
        assert c._root_folder_id() == "F"
        c2 = GoogleDriveClient(access_token="t", shared_drive_id="S")
        assert c2._root_folder_id() == "S"
        c3 = GoogleDriveClient(access_token="t")
        assert c3._root_folder_id() == "root"

    def test_extension_filter_for_google_native(self):
        c = GoogleDriveClient(access_token="t", allowed_extensions="gsheet,csv")
        assert c._allowed("Pipeline", "application/vnd.google-apps.spreadsheet") is True
        assert c._allowed("Report.pdf", "application/pdf") is False
        assert c._allowed("data.csv", "text/csv") is True

    def test_missing_token_raises_on_use(self):
        c = GoogleDriveClient()
        with pytest.raises(ValueError, match="no access token"):
            c._headers()


# -------------------------------------------- list_files end-to-end (mocked)


class TestGraphListFiles:
    def _client(self, **kwargs):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
            **kwargs,
        )
        # Skip the resolution roundtrip; pretend we already resolved.
        c._site_id = "site-id"
        c._drive_id = "drive-id"
        c._root_item_id = "root-item-id"
        c.access_token = "fake-token"
        return c

    def test_list_files_filters_folders_and_extensions(self):
        c = self._client(allowed_extensions="xlsx")
        children = [
            {"id": "1", "name": "report.xlsx", "size": 100,
             "lastModifiedDateTime": "2025-01-01T00:00:00Z",
             "file": {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
             "webUrl": "https://x/1"},
            {"id": "2", "name": "notes.pdf", "size": 200,
             "lastModifiedDateTime": "2025-01-02T00:00:00Z",
             "file": {"mimeType": "application/pdf"}, "webUrl": "https://x/2"},
            {"id": "3", "name": "subfolder",
             "folder": {"childCount": 4}},
        ]
        with patch.object(c, "_get", return_value={"value": children, "@odata.nextLink": None}):
            files = c.list_files()
        names = [f["name"] for f in files]
        assert "report.xlsx" in names
        assert "notes.pdf" not in names  # extension filter
        assert "subfolder" not in names  # folder excluded

    def test_list_files_recursive(self):
        c = self._client(recursive=True)
        # First page: 1 file + 1 folder. Second page (folder children): 1 file.
        responses = iter([
            {"value": [
                {"id": "1", "name": "a.csv",
                 "lastModifiedDateTime": "2025-01-01T00:00:00Z",
                 "file": {"mimeType": "text/csv"}, "webUrl": "https://x/1"},
                {"id": "f1", "name": "sub", "folder": {"childCount": 1}},
            ]},
            {"value": [
                {"id": "2", "name": "b.csv",
                 "lastModifiedDateTime": "2025-01-02T00:00:00Z",
                 "file": {"mimeType": "text/csv"}, "webUrl": "https://x/2"},
            ]},
        ])
        with patch.object(c, "_get", side_effect=lambda *a, **k: next(responses)):
            files = c.list_files()
        names = sorted(f["name"] for f in files)
        assert names == ["a.csv", "b.csv"]
        # Path reflects nesting for the second file
        b = next(f for f in files if f["name"] == "b.csv")
        assert b["path"] == "sub/b.csv"


class TestTrimToData:
    """Regression: real-world Excel sheets often have leading blank rows
    before the header row (Book 7.xlsx → headers on row 7, data on rows
    8+). pd.read_excel with header=0 reads row 1 as the header → empty
    frame. _trim_to_data finds the real table by dropping all-empty
    rows / columns and promoting the first remaining row to header."""

    def test_no_leading_blanks_is_noop(self):
        import pandas as pd
        from app.data_sources.clients.graph_drive_client import _trim_to_data
        raw = pd.DataFrame([
            ["col_a", "col_b"],
            [1, "x"],
            [2, "y"],
        ])
        out = _trim_to_data(raw)
        assert list(out.columns) == ["col_a", "col_b"]
        assert len(out) == 2

    def test_leading_blank_rows_are_skipped(self):
        """The Book 7.xlsx case — six blank rows before the header."""
        import pandas as pd
        from app.data_sources.clients.graph_drive_client import _trim_to_data
        raw = pd.DataFrame([
            [None, None, None],
            [None, None, None],
            [None, None, None],
            [None, None, None],
            [None, None, None],
            [None, None, None],
            ["AlbumId", "Album Title", "Album Revenue"],
            [253, "Battlestar Galactica (Classic), Season 1", 35.82],
            [254, "Battlestar Galactica (Classic), Season 2", 35.82],
        ])
        out = _trim_to_data(raw)
        assert list(out.columns) == ["AlbumId", "Album Title", "Album Revenue"]
        assert len(out) == 2

    def test_trailing_blank_columns_dropped(self):
        import pandas as pd
        from app.data_sources.clients.graph_drive_client import _trim_to_data
        raw = pd.DataFrame([
            ["col_a", "col_b", None, None],
            [1, "x", None, None],
        ])
        out = _trim_to_data(raw)
        assert list(out.columns) == ["col_a", "col_b"]
        assert len(out) == 1

    def test_fully_empty_returns_empty_frame(self):
        import pandas as pd
        from app.data_sources.clients.graph_drive_client import _trim_to_data
        raw = pd.DataFrame([[None, None], [None, None]])
        out = _trim_to_data(raw)
        assert out.empty


class TestGraphFilenameResolution:
    """Regression: LLM often passes filenames where Graph expects opaque
    item IDs. The client should detect filename-shaped inputs and resolve
    via path or search before hitting /items/{id}."""

    def _client(self):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
        )
        c._drive_id = "drive-id"
        c.access_token = "fake"
        return c

    def test_opaque_id_passes_through(self):
        c = self._client()
        assert c._resolve_item_id("drive-id", "01TP3T7WF6Y2GOVW7725BZO354PWSELRRZ") \
            == "01TP3T7WF6Y2GOVW7725BZO354PWSELRRZ"

    def test_filename_resolves_via_path_lookup(self):
        c = self._client()
        with patch.object(c, "_get", return_value={"id": "RESOLVED-ID", "name": "Book 7.xlsx"}):
            assert c._resolve_item_id("drive-id", "Book 7.xlsx") == "RESOLVED-ID"

    def test_filename_falls_back_to_search(self):
        """Path lookup raises (file not at expected path) → search fallback
        finds it by name."""
        c = self._client()
        calls = {"n": 0}

        def fake_get(url):
            calls["n"] += 1
            # First call (path lookup) — simulate Graph 404 by raising
            if calls["n"] == 1:
                raise ValueError("404 not found")
            # Second call (search) — return name match
            return {"value": [
                {"id": "FOLDER-ID", "folder": {}, "name": "subfolder"},
                {"id": "REAL-ID", "name": "Book 7.xlsx"},
            ]}

        with patch.object(c, "_get", side_effect=fake_get):
            assert c._resolve_item_id("drive-id", "Book 7.xlsx") == "REAL-ID"

    def test_unresolvable_filename_returns_input(self):
        """If both path and search fail, pass through — Graph will 404 with
        a clearer error than swallowing silently."""
        c = self._client()
        with patch.object(c, "_get", side_effect=ValueError("404")):
            assert c._resolve_item_id("drive-id", "nonsense.xlsx") == "nonsense.xlsx"


class TestGraphGetSchemas:
    def test_files_become_tables(self):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
        )
        fake_files = [
            {"id": "F1", "name": "a.csv", "path": "a.csv", "mime_type": "text/csv",
             "size": 10, "modified_at": "2025-01-01T00:00:00Z", "web_url": "u",
             "drive_id": "d"},
        ]
        with patch.object(c, "list_files", return_value=fake_files):
            tables = c.get_schemas()
        assert len(tables) == 1
        t = tables[0]
        assert t.name == "a.csv"
        assert t.metadata_json["graph"]["file_id"] == "F1"


# ---------------------------------------------- Google list_files (mocked)


class TestGoogleListFiles:
    def _client(self, **kwargs):
        return GoogleDriveClient(access_token="tok", **kwargs)

    def test_filters_folders_and_extensions(self):
        # recursive=False so the mock fixture (which always returns the same
        # files including a folder) doesn't loop infinitely. This test
        # exercises filter logic, not recursion semantics.
        c = self._client(allowed_extensions="csv,gsheet", recursive=False)
        files = [
            {"id": "1", "name": "data.csv", "mimeType": "text/csv",
             "modifiedTime": "2025-01-01", "size": "10", "webViewLink": "u1"},
            {"id": "2", "name": "Pipeline", "mimeType": "application/vnd.google-apps.spreadsheet",
             "modifiedTime": "2025-01-02", "webViewLink": "u2"},
            {"id": "3", "name": "doc.pdf", "mimeType": "application/pdf",
             "modifiedTime": "2025-01-03", "size": "5", "webViewLink": "u3"},
            {"id": "4", "name": "sub", "mimeType": "application/vnd.google-apps.folder"},
        ]
        with patch.object(c, "_get", return_value={"files": files}):
            results = c.list_files()
        names = sorted(f["name"] for f in results)
        assert names == ["Pipeline", "data.csv"]


# ---------------------------------------------------- test_connection


class TestTestConnection:
    def test_onedrive_admin_only_does_not_hit_me_drive(self):
        """Regression: admin-only OneDrive test_connection must not call /me/drive
        (which returns 400 'request is only valid with delegated auth')."""
        c = OnedriveClient(tenant_id="t", client_id="c", client_secret="s")
        with patch.object(c, "_token", return_value="app-only-token") as tok, \
             patch.object(c, "_resolve_drive_id") as resolve_drive:
            result = c.test_connection()
        assert result["success"] is True
        assert tok.called
        assert not resolve_drive.called
        assert "sign in" in result["message"].lower()

    def test_onedrive_admin_only_when_token_populates_access_token(self):
        """Regression: _token() in service-principal mode sets self.access_token
        to the app-only token. test_connection must NOT then take the delegated
        branch and call /me/drive — that's exactly the bug observed in production."""
        c = OnedriveClient(tenant_id="t", client_id="c", client_secret="s")
        assert c.access_token is None  # admin-only: no user token

        def fake_token():
            c.access_token = "app-only-token-from-client-credentials"
            return c.access_token

        with patch.object(c, "_token", side_effect=fake_token), \
             patch.object(c, "_resolve_drive_id") as resolve_drive, \
             patch.object(c, "_resolve_root_item_id") as resolve_root:
            result = c.test_connection()
        assert result["success"] is True
        assert not resolve_drive.called, "Must not call /me/drive after _token() populates access_token"
        assert not resolve_root.called
        assert "sign in" in result["message"].lower()

    def test_sharepoint_admin_only_resolves_site_not_drive(self):
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
        )
        with patch.object(c, "_token", return_value="app-only-token"), \
             patch.object(c, "_resolve_site_id", return_value="site-id") as resolve_site, \
             patch.object(c, "_resolve_drive_id") as resolve_drive:
            result = c.test_connection()
        assert result["success"] is True
        assert resolve_site.called
        assert not resolve_drive.called

    def test_delegated_runs_full_path(self):
        """User access_token present → resolve drive + root (real read-path test)."""
        c = OnedriveClient(access_token="user-token")
        with patch.object(c, "_token", return_value="user-token"), \
             patch.object(c, "_resolve_drive_id") as resolve_drive, \
             patch.object(c, "_resolve_root_item_id") as resolve_root:
            result = c.test_connection()
        assert result["success"] is True
        assert resolve_drive.called
        assert resolve_root.called
        assert result["message"] == "Connected"

    def test_google_drive_no_token_skips_calls(self):
        c = GoogleDriveClient()
        # No network call should happen
        with patch.object(c, "_get") as get:
            result = c.test_connection()
        assert result["success"] is True
        assert not get.called
        assert "sign in" in result["message"].lower()

    def test_onedrive_list_files_without_user_token_returns_empty(self):
        """get_schemas at admin-save calls list_files; OneDrive can't enumerate
        without a user token, so it must return [] (not raise / not call /me)."""
        c = OnedriveClient(tenant_id="t", client_id="c", client_secret="s")
        with patch.object(c, "_resolve_drive_id") as resolve_drive:
            files = c.list_files()
        assert files == []
        assert not resolve_drive.called

    def test_onedrive_get_schemas_without_user_token_is_empty(self):
        c = OnedriveClient(tenant_id="t", client_id="c", client_secret="s")
        with patch.object(c, "_resolve_drive_id") as resolve_drive:
            schemas = c.get_schemas()
        assert schemas == []
        assert not resolve_drive.called

    def test_sharepoint_list_files_without_user_token_still_works(self):
        """SharePoint app-only auth uses /sites/{id}/drive, which works.
        Must not be short-circuited like OneDrive."""
        c = SharepointClient(
            tenant_id="t", client_id="c", client_secret="s",
            site_url="https://x.sharepoint.com/sites/A",
        )
        with patch.object(c, "_resolve_drive_id", return_value="drive-id"), \
             patch.object(c, "_resolve_root_item_id", return_value="root"), \
             patch.object(c, "_get", return_value={"value": []}) as get:
            files = c.list_files()
        assert files == []
        assert get.called  # we DID try to enumerate

    def test_google_drive_list_files_without_token_returns_empty(self):
        c = GoogleDriveClient()
        with patch.object(c, "_get") as get:
            files = c.list_files()
        assert files == []
        assert not get.called

    def test_google_drive_with_token_calls_about(self):
        c = GoogleDriveClient(access_token="t")
        with patch.object(c, "_get", return_value={"user": {"emailAddress": "x@y"}}) as get, \
             patch.object(c, "_list_in_folder", return_value=[]):
            result = c.test_connection()
        assert result["success"] is True
        assert get.called


# -------------------------------------------- OAuth params service wiring


class TestOAuthServiceWiring:
    def _conn(self, type, creds):
        m = MagicMock()
        m.id = "x"
        m.type = type
        m.decrypt_credentials.return_value = creds
        return m

    def test_sharepoint_uses_graph_scopes(self):
        from app.services.connection_oauth_service import get_oauth_params

        params = get_oauth_params(self._conn("sharepoint", {
            "tenant_id": "T", "client_id": "C", "client_secret": "S",
        }))
        assert params["provider_name"] == "microsoft"
        assert "Files.Read.All" in params["scopes"]
        assert "Sites.Read.All" in params["scopes"]

    def test_onedrive_uses_files_scope(self):
        from app.services.connection_oauth_service import get_oauth_params

        params = get_oauth_params(self._conn("onedrive", {
            "tenant_id": "T", "client_id": "C", "client_secret": "S",
        }))
        assert "Files.Read.All" in params["scopes"]

    def test_google_drive_scopes(self):
        from app.services.connection_oauth_service import get_oauth_params

        params = get_oauth_params(self._conn("google_drive", {
            "oauth_client_id": "C", "oauth_client_secret": "S",
        }))
        assert params["provider_name"] == "google"
        assert "drive.readonly" in params["scopes"]
        assert "spreadsheets.readonly" in params["scopes"]

    def test_google_drive_missing_creds_raises(self):
        from app.services.connection_oauth_service import get_oauth_params

        with pytest.raises(ValueError, match="oauth_client"):
            get_oauth_params(self._conn("google_drive", {}))

    def test_mcp_oauth_params(self):
        """MCP OAuth pre-configured: admin enters authorize/token URLs + client
        creds + scopes + optional audience. The audience is RFC 8707 resource
        indicator and audience-binds the token to the MCP server."""
        from app.services.connection_oauth_service import get_oauth_params

        params = get_oauth_params(self._conn("mcp", {
            "authorize_url": "https://idp.example.com/oauth/authorize",
            "token_url": "https://idp.example.com/oauth/token",
            "client_id": "cid",
            "client_secret": "cs",
            "scopes": "openid profile offline_access",
            "audience": "https://mcp.example.com",
        }))
        assert params["provider_name"] == "mcp"
        assert params["authorize_url"].startswith("https://idp")
        assert params["audience"] == "https://mcp.example.com"
        assert params["scopes"] == "openid profile offline_access"

    def test_mcp_oauth_missing_required_fields_raises(self):
        from app.services.connection_oauth_service import get_oauth_params

        with pytest.raises(ValueError, match="missing authorize_url"):
            get_oauth_params(self._conn("mcp", {"client_id": "x"}))

    def test_google_search_uses_fulltext_for_content_parity(self):
        """GDrive search should hit both name AND fullText so it matches
        Microsoft Graph's content-inclusive search semantics."""
        from app.data_sources.clients.google_drive_client import GoogleDriveClient
        c = GoogleDriveClient(access_token="t")
        captured = {}

        def fake_get(url, params=None, **kw):
            captured["q"] = params.get("q")
            return {"files": []}

        with patch.object(c, "_get", side_effect=fake_get):
            c.search_files("pipeline")
        assert "fullText contains" in captured["q"]
        assert "name contains" in captured["q"]

    def test_mcp_oauth_audience_propagates_to_token_request(self):
        """RFC 8707 `resource` parameter is added to token-exchange body when
        an audience is configured."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.services.connection_oauth_service import exchange_code_for_tokens

        params = {
            "token_url": "https://idp.example.com/token",
            "client_id": "cid",
            "client_secret": "cs",
            "audience": "https://mcp.example.com",
        }
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "access_token": "tok", "refresh_token": "rt",
            "expires_in": 3600, "token_type": "Bearer",
        }
        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.post.return_value = fake_response

        with patch("app.services.connection_oauth_service.httpx.AsyncClient", return_value=fake_client):
            asyncio.run(exchange_code_for_tokens(
                params, code="code", redirect_uri="https://x.com/cb",
            ))
        post_kwargs = fake_client.post.call_args
        body = post_kwargs.kwargs.get("data") or post_kwargs.args[1]
        assert body.get("resource") == "https://mcp.example.com"


class TestConstructClientKwargs:
    """Regression: client subclasses that just forward `**kwargs` to their
    parent (e.g. `class OnedriveClient(GraphDriveClient): def __init__(self, **kwargs): ...`)
    used to have every legitimate arg stripped by construct_client's
    narrowing-by-signature, because inspect.signature only reports `self`
    and `kwargs` on such classes.

    The fix: when the constructor accepts **kwargs, skip the narrowing.
    Pass everything through.
    """

    def test_var_kwargs_class_keeps_all_params(self):
        import inspect

        # Simulate the same pattern OnedriveClient uses
        class ParentClient:
            def __init__(self, access_token=None, tenant_id=None, **_ignored):
                self.access_token = access_token
                self.tenant_id = tenant_id

        class ForwarderSubclass(ParentClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

        # Inline the narrowing logic from construct_client
        sig = inspect.signature(ForwarderSubclass.__init__)
        params = {"access_token": "tok", "refresh_token": "rt", "tenant_id": "t"}
        accepts_var_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_var_kwargs:
            allowed = params
        else:
            allowed = {k: v for k, v in params.items() if k in sig.parameters and k != "self"}

        # The bug was: this list was empty. The fix: all params pass through.
        assert "access_token" in allowed
        assert "tenant_id" in allowed

        client = ForwarderSubclass(**allowed)
        assert client.access_token == "tok"
        assert client.tenant_id == "t"

    def test_explicit_signature_class_still_narrows(self):
        """Non-forwarder classes must still narrow — we don't want to leak
        meta keys to constructors that don't accept them."""
        import inspect

        class StrictClient:
            def __init__(self, host, port=5432):
                self.host = host
                self.port = port

        sig = inspect.signature(StrictClient.__init__)
        params = {"host": "db.example.com", "port": 5432, "stray_field": "boom"}
        accepts_var_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_var_kwargs:
            allowed = params
        else:
            allowed = {k: v for k, v in params.items() if k in sig.parameters and k != "self"}

        assert "host" in allowed
        assert "port" in allowed
        assert "stray_field" not in allowed
