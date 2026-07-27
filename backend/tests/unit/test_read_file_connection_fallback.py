"""read_file: a file-source id passed WITHOUT connection_id.

Production trace this came from — the agent ran list_files on a SharePoint
connection, got back Graph item ids, then called read_file with only
`file_id='01LZCX…'`. An empty connection_id routes to the report's own file
space (uploads / attach_file results), the Graph id isn't there, and the read
failed with "is not a file attached to this conversation". The model read that
as "my id is wrong", re-ran list_files, got the same id back, and looped.

The id was fine — only the source was unnamed. So: fall through to the agent's
file connection when there is exactly one, WITHOUT relaxing any access check
(the user must still be able to reach that data source, and it must declare the
capability), and when a fallback isn't safe, say which connections exist so the
next call names one instead of re-listing.
"""
from __future__ import annotations

import json
import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.data_sources.clients.base import Capability


# A real Microsoft Graph driveItem id — opaque base32, no dots or slashes, and
# emphatically not a uuid4. This is the shape the fallback keys on.
GRAPH_ID = "01LZCX6LHDIF2ODN4WMRDIZQRZ72A77RUD"


class _StubClient:
    """Minimal file client: enough surface for a whole-file text read."""

    def __init__(self, text: str = "ITURAN-REVENUE-2026", caps=None):
        self.capabilities = {Capability.READ_FILE} if caps is None else caps
        self.text = text
        self.reads: list = []

    async def afile_version(self, file_id: str):
        return None  # no cheap version → skip the content cache entirely

    async def aread_file(self, file_id: str, **kwargs):
        self.reads.append(file_id)
        return self.text


def _conn(conn_id: str, name: str, conn_type: str = "sharepoint"):
    c = MagicMock()
    c.id = conn_id
    c.name = name
    c.type = conn_type
    c.is_active = True
    c.auth_policy = "system"
    c.allowed_user_auth_modes = []
    return c


def _ctx(*conns, files=None):
    ds = MagicMock()
    ds.id = "DS-1"
    ds.name = "Company Files"
    ds.is_active = True
    ds.connections = list(conns)

    report = MagicMock()
    report.id = "REP-1"
    report.data_sources = [ds] if conns else []
    report.files = files or []

    org = MagicMock()
    org.id = "ORG-1"
    org.settings = None

    return {
        "db": AsyncMock(),
        "organization": org,
        "report": report,
        "user": MagicMock(id="USER-1"),
        "model": MagicMock(supports_vision=False),
    }


async def _run_read(tool_input, runtime_ctx, client=None):
    """Drive ReadFileTool with ConnectionService stubbed out. Returns the final
    payload; the caller inspects `client.reads` to see if the connector was hit."""
    from app.ai.tools.implementations.read_file import ReadFileTool

    with patch("app.services.connection_service.ConnectionService") as svc_cls, patch(
        "app.ai.tools.implementations.read_file.attach_drive_file_to_session",
        new=AsyncMock(return_value="SESSION-1"),
    ):
        svc_cls.return_value.construct_client = AsyncMock(return_value=client)
        events = [e async for e in ReadFileTool().run_stream(tool_input, runtime_ctx)]
    return events[-1].payload


# --------------------------------------------------------------- fallback


class TestImplicitConnectionFallback:
    @pytest.mark.asyncio
    async def test_graph_id_without_connection_id_reads_the_only_connection(self):
        client = _StubClient()
        ctx = _ctx(_conn("CONN-1", "Employees SharePoint"))
        payload = await _run_read({"connection_id": "", "file_id": GRAPH_ID}, ctx, client)

        out = payload["output"]
        assert out["success"] is True, out.get("error")
        assert client.reads == [GRAPH_ID]
        # The read reports the connection it actually came from — a blank id here
        # would misfile a connector read as a conversation attachment (and would
        # poison the content cache key).
        assert out["connection_id"] == "CONN-1"
        assert "ITURAN-REVENUE-2026" in (payload["observation"].get("details") or "")

    @pytest.mark.asyncio
    async def test_path_shaped_id_also_falls_through(self):
        """network_dir / S3 ids are paths, not opaque tokens — same fallback."""
        client = _StubClient()
        ctx = _ctx(_conn("CONN-1", "Shared drive", conn_type="network_dir"))
        payload = await _run_read(
            {"connection_id": "", "file_id": "reports/q3/ituran.txt"}, ctx, client
        )
        assert payload["output"]["success"] is True
        assert client.reads == ["reports/q3/ituran.txt"]


# ----------------------------------------------------------- access guards


class TestFallbackStillChecksAccess:
    @pytest.mark.asyncio
    async def test_user_without_data_source_access_is_denied(self):
        """The fallback picks the connection; it does NOT grant reach to it.
        A user who can't access the data source gets nothing back."""
        client = _StubClient(text="CONFIDENTIAL-PAYROLL")
        ctx = _ctx(_conn("CONN-1", "HR SharePoint"))
        with patch(
            "app.core.permission_resolver.user_can_access_data_source",
            new=AsyncMock(return_value=False),
        ):
            payload = await _run_read({"connection_id": "", "file_id": GRAPH_ID}, ctx, client)

        out = payload["output"]
        assert out["success"] is False
        assert "do not have access" in out["error"]
        assert client.reads == []  # never reached the provider
        assert "CONFIDENTIAL-PAYROLL" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_capability_is_still_required(self):
        client = _StubClient(caps={Capability.LIST_FILES})
        ctx = _ctx(_conn("CONN-1", "Employees SharePoint"))
        payload = await _run_read({"connection_id": "", "file_id": GRAPH_ID}, ctx, client)

        assert payload["output"]["success"] is False
        assert "does not support read_file" in payload["output"]["error"]

    @pytest.mark.asyncio
    async def test_inactive_connection_is_not_a_fallback_target(self):
        conn = _conn("CONN-1", "Employees SharePoint")
        conn.is_active = False
        client = _StubClient()
        payload = await _run_read({"connection_id": "", "file_id": GRAPH_ID}, _ctx(conn), client)

        assert payload["output"]["success"] is False
        assert client.reads == []


# -------------------------------------------------------- unresolved errors


class TestUnresolvedErrorGuidesTheRetry:
    @pytest.mark.asyncio
    async def test_two_connections_are_named_instead_of_guessed(self):
        client = _StubClient()
        ctx = _ctx(
            _conn("CONN-1", "Employees SharePoint"),
            _conn("CONN-2", "Finance OneDrive", conn_type="onedrive"),
        )
        payload = await _run_read({"connection_id": "", "file_id": GRAPH_ID}, ctx, client)

        err = payload["output"]["error"]
        assert payload["output"]["success"] is False
        assert client.reads == []  # ambiguous — must not pick one at random
        # The retry the model should make is spelled out, with real ids.
        assert "connection_id" in err
        assert "CONN-1" in err and "CONN-2" in err
        assert "Employees SharePoint" in err and "Finance OneDrive" in err
        # ...and the move that caused the production loop is ruled out.
        assert "NOT re-run list_files" in err

    @pytest.mark.asyncio
    async def test_stale_session_uuid_does_not_probe_the_connector(self):
        """A uuid4 is a conversation-attachment id by construction. If it isn't
        in this report's space it's stale or foreign — reporting that beats a
        provider 404 from a connector that was never going to hold it."""
        client = _StubClient()
        ctx = _ctx(_conn("CONN-1", "Employees SharePoint"))
        payload = await _run_read(
            {"connection_id": "", "file_id": str(_uuid.uuid4())}, ctx, client
        )

        assert payload["output"]["success"] is False
        assert client.reads == []
        assert "another report" in payload["output"]["error"]

    @pytest.mark.asyncio
    async def test_no_file_source_attached_says_so(self):
        payload = await _run_read({"connection_id": "", "file_id": GRAPH_ID}, _ctx(), None)

        err = payload["output"]["error"]
        assert payload["output"]["success"] is False
        assert "no file source is attached" in err
        assert "<files>" in err


# --------------------------------------------------------------- precedence


class TestSessionSpaceStillWins:
    @pytest.mark.asyncio
    async def test_attached_file_beats_the_connection(self, tmp_path):
        """The report's own space is still checked first — a session file must
        not be re-fetched from a connector just because one is attached."""
        path = tmp_path / "notes.txt"
        path.write_bytes(b"SESSION-SIDE-CONTENT")
        f = MagicMock()
        f.id = str(_uuid.uuid4())
        f.filename = "notes.txt"
        f.path = str(path)
        f.organization_id = "ORG-1"

        client = _StubClient()
        ctx = _ctx(_conn("CONN-1", "Employees SharePoint"), files=[f])
        payload = await _run_read({"connection_id": "", "file_id": f.id}, ctx, client)

        out = payload["output"]
        assert out["success"] is True, out.get("error")
        assert client.reads == []
        assert out["file_name"] == "notes.txt"
        assert "SESSION-SIDE-CONTENT" in (payload["observation"].get("details") or "")
