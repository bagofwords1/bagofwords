"""SharePoint 2010 live search stays bounded: it stops crawling at its deadline,
when its caller is cancelled, and at a folder cap, and the search_files tool
still answers (with partial results marked incomplete) instead of hanging.

Only the SOAP server and the clock are simulated (see test_sharepoint_2010_client).
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.ai.tools.implementations import search_files as search_tool_module
from app.ai.tools.implementations.search_files import SearchFilesTool
from app.data_sources.clients import _sharepoint_2010
from app.data_sources.clients.base import SearchStopped
from app.models.connection import Connection
from tests.unit.test_sharepoint_2010_client import ROOT, SITE, client, legacy  # noqa: F401


def big_library(server, folders, files_per_folder):
    """A library with `folders` sibling folders, each holding a few reports."""
    server.blobs = {
        f'{ROOT}/case-{f:03}/report-{f:03}-{i}.txt': b'x'
        for f in range(folders) for i in range(files_per_folder)
    }
    server.empty_folders = set()
    # The root lists every folder in one page; each folder is one request.
    server.page_size = 200


class FakeClock:
    """Advances one second per SOAP request, so a deadline means 'N requests'."""

    def __init__(self, server):
        self.now = 1000.0
        server.override = self._tick

    def _tick(self, op, command):
        self.now += 1.0
        return None  # let the server answer normally

    def monotonic(self):
        return self.now


def list_item_calls(server):
    return server.calls.count('GetListItems')


@pytest.mark.parametrize('folders,budget', [(30, 6), (60, 15)])
def test_live_search_stops_crawling_at_its_deadline(legacy, monkeypatch, folders, budget):  # noqa: F811
    big_library(legacy, folders, 3)
    clock = FakeClock(legacy)
    monkeypatch.setattr(_sharepoint_2010, 'time', SimpleNamespace(monotonic=clock.monotonic), raising=False)
    c = client(drive_name='Documents', recursive=True)

    full_walk = list_item_calls(legacy)
    c.list_files()  # how many requests a complete walk takes
    full_walk = list_item_calls(legacy) - full_walk

    before = list_item_calls(legacy)
    with pytest.raises(SearchStopped) as stopped:
        c.search_files('report', limit=500, deadline=clock.monotonic() + budget)
    made = list_item_calls(legacy) - before
    assert stopped.value.stop_reason == 'deadline'
    # It stops at the first checkpoint past the deadline, far short of the walk.
    assert made <= budget + 1 < full_walk
    # Matches found before the stop are returned, not discarded.
    assert stopped.value.files and all('report' in f['path'] for f in stopped.value.files)


def test_live_search_without_bounds_hit_returns_every_match(legacy):  # noqa: F811
    big_library(legacy, 8, 2)
    c = client(drive_name='Documents', recursive=True)
    found = c.search_files('report-003', limit=500, deadline=None)
    assert sorted(f['path'] for f in found) == ['case-003/report-003-0.txt', 'case-003/report-003-1.txt']


@pytest.mark.parametrize('limit', [2, 5])
def test_live_search_stops_at_the_folder_cap(legacy, limit):  # noqa: F811
    big_library(legacy, 20, 1)
    c = client(drive_name='Documents', recursive=True)
    c._adapter.search_folder_limit = limit
    before = list_item_calls(legacy)
    with pytest.raises(SearchStopped) as stopped:
        c.search_files('report', limit=500)
    assert stopped.value.stop_reason == 'folder_limit'
    # One request per folder here (each holds a single file): never past the cap.
    assert list_item_calls(legacy) - before <= limit


def test_cancelled_live_search_stops_its_worker_thread(legacy):  # noqa: F811
    """The tool runner cancels the awaiting task on timeout; the thread doing
    the SOAP walk must notice and stop instead of crawling on unobserved."""
    big_library(legacy, 40, 2)
    c = client(drive_name='Documents', recursive=True)
    c._adapter._libraries()  # discovery up front; only the walk is measured
    in_flight, gate = threading.Event(), threading.Event()

    def hold_first_folder(op, command):
        if op == 'GetListItems' and not in_flight.is_set():
            in_flight.set()
            gate.wait(10)
        return None

    legacy.override = hold_first_folder

    async def scenario():
        task = asyncio.create_task(c.asearch_files('report'))
        assert await asyncio.to_thread(in_flight.wait, 10)
        before = list_item_calls(legacy)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        # Waits for the worker thread to finish whatever it still does.
        await asyncio.get_running_loop().shutdown_default_executor()
        return list_item_calls(legacy) - before

    after_cancel = asyncio.run(scenario())
    # At most the in-flight request's own follow-up page; not the other folders.
    assert after_cancel <= 1


def _runtime():
    conn = Connection(id='sp2010-connection', name='Legacy documents', type='sharepoint_onprem', is_active=True,
                      auth_policy='system_only', allowed_user_auth_modes=[],
                      config={'site_url': SITE, 'drive_name': 'Documents', 'api_mode': '2010',
                              'recursive': True, 'index_mode': 'none'})
    conn.encrypt_credentials({'username': 'DOMAIN\\reader', 'password': 'synthetic'})
    ds = SimpleNamespace(id='sp2010-agent', name='Legacy', is_active=True, connections=[conn])
    # Transient models; no catalog is reachable, so the tool must search live.
    return {'db': object(), 'organization': SimpleNamespace(id='synthetic-org'),
            'report': SimpleNamespace(data_sources=[ds])}


def test_search_tool_answers_with_partial_results_when_live_search_times_out(legacy, monkeypatch):  # noqa: F811
    big_library(legacy, 50, 2)
    clock = FakeClock(legacy)
    fake_time = SimpleNamespace(monotonic=clock.monotonic)
    monkeypatch.setattr(_sharepoint_2010, 'time', fake_time, raising=False)
    monkeypatch.setattr(search_tool_module, 'time', fake_time, raising=False)

    async def run():
        return [e async for e in SearchFilesTool().run_stream(
            {'connection_id': 'sp2010-connection', 'query': 'report', 'max_results': 500}, _runtime())]

    events = asyncio.run(run())
    end = events[-1]
    assert end.type == 'tool.end'
    out = end.payload['output']
    assert out['success'] and out['incomplete'] and out['stop_reason'] == 'deadline'
    assert out['source'] == 'live'
    assert 0 < out['file_count'] < 100  # partial: the library holds 100 reports
    assert end.payload['observation']['success']
