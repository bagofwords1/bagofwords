"""Progress callbacks share one contract across connector implementations."""
import ast
from pathlib import Path
import inspect
import pytest

CLIENTS = Path(__file__).resolve().parents[2] / 'app/data_sources/clients'


def test_direct_callbacks_follow_indexing_contract():
    def callback(phase, current_item, done, total): pass
    signature = inspect.signature(callback)
    failures = []
    for path in CLIENTS.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'progress_callback':
                try:
                    signature.bind(*[None for _ in node.args], **{k.arg: None for k in node.keywords})
                except TypeError:
                    failures.append(f'{path.name}:{node.lineno}')
    assert not failures, f'Incompatible callbacks: {failures}'

from app.data_sources.clients.progress import discovery_progress, discovery_items, discovery_phase, IndexingCancelled

@pytest.mark.parametrize('count', [0, 1, 10000, 35000])
@pytest.mark.parametrize('known_total', [False, True])
def test_reporting_preserves_lazy_catalog_and_counts(count, known_total):
    reads = []
    @discovery_progress
    def discover(progress_callback=None):
        discovery_phase('reading_metadata')
        def source():
            for i in range(count):
                reads.append(i)
                yield i
        return list(discovery_items(source(), 'tables', total=count if known_total else None, label=lambda i: f'table_{i}'))
    events = []
    actual = discover(progress_callback=lambda *event: events.append(event))
    assert actual == list(range(count))
    assert reads == actual
    assert events[0][0] == 'discovering_schema'
    assert events[-1][0] == 'catalog_ready'
    assert events[-1][2:] == (count, count)
    assert all(done >= 0 and (total == 0 or done <= total) for _, _, done, total in events)


def test_cancellation_stops_iteration_and_context_does_not_leak():
    consumed = []
    @discovery_progress
    def discover(progress_callback=None):
        for i in discovery_items(range(100), 'tables'):
            consumed.append(i)
        return consumed
    def cancel(phase, item, done, total):
        if phase == 'tables' and done == 3:
            raise IndexingCancelled()
    with pytest.raises(IndexingCancelled):
        discover(progress_callback=cancel)
    assert consumed == [0, 1, 2]
    consumed.clear()
    assert len(discover()) == 100


def test_reporter_failures_do_not_change_results():
    @discovery_progress
    def discover(progress_callback=None):
        return list(discovery_items([1, 2, 3], 'tables'))
    def broken(*args): raise RuntimeError('telemetry unavailable')
    assert discover(progress_callback=broken) == [1, 2, 3]


def test_every_registered_catalog_can_cancel_before_external_io():
    from app.schemas.data_source_registry import list_available_data_sources, resolve_client_class
    errors = []
    for entry in list_available_data_sources():
        if entry['data_shape'] == 'tools':
            continue
        cls = resolve_client_class(entry['type'])
        def cancel(*event):
            assert len(event) == 4
            raise IndexingCancelled()
        # Deliberately no initialized client: cancellation at discovery entry
        # must precede any credential access, network I/O, or driver state.
        try:
            cls.get_schemas(None, progress_callback=cancel)
        except IndexingCancelled:
            continue
        except Exception as exc:
            errors.append((entry['type'], type(exc).__name__))
        else:
            errors.append((entry['type'], 'did not cancel'))
    assert errors == []


def test_parallel_discoveries_keep_their_callbacks_isolated():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    @discovery_progress
    def discover(name, progress_callback=None):
        barrier.wait(timeout=10)
        return list(discovery_items([name], 'tables', label=str))
    left, right = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(discover, 'left', progress_callback=lambda *e: left.append(e))
        b = pool.submit(discover, 'right', progress_callback=lambda *e: right.append(e))
        assert a.result() == ['left']
        assert b.result() == ['right']
    assert {e[1] for e in left if e[1]} == {'left'}
    assert {e[1] for e in right if e[1]} == {'right'}


def test_large_catalog_emissions_are_bounded_with_exact_completion(monkeypatch):
    from app.data_sources.clients.progress import make_reporter
    monkeypatch.setattr('app.data_sources.clients.progress.time.monotonic', lambda: 10.0)
    events = []
    reporter = make_reporter(lambda *e: events.append(e))
    reporter.phase('tables', total=35000)
    for i in range(35000):
        reporter.item(f'table_{i}', done=i + 1)
    reporter.done()
    assert len(events) <= 103
    assert events[-1][2:] == (35000, 35000)


def test_powerbi_reporting_preserves_incremental_catalog_and_http_cost():
    from unittest.mock import Mock
    from app.data_sources.clients.powerbi_client import PowerBIClient

    prior = {'Sales/Orders': {
        'columns': [{'name': 'Id', 'dtype': 'int'}], 'pks': [], 'fks': [],
        'metadata_json': {'powerbi': {'datasetId': 'model-1', 'tableName': 'Orders'}},
    }}
    def discover(callback):
        client = PowerBIClient(tenant_id='tenant', client_id='app', client_secret='secret')
        client._access_token = 'synthetic-token'
        requests = []
        def request(method, url, **kwargs):
            requests.append((method, url))
            if url.endswith('/groups'):
                payload = {'value': [{'id': 'workspace-1', 'name': 'Finance'}]}
            elif url.endswith('/datasets'):
                payload = {'value': [{'id': 'model-1', 'name': 'Sales'}]}
            elif url.endswith('/reports'):
                payload = {'value': []}
            else:
                raise AssertionError(f'Unexpected Power BI request: {method} {url}')
            response = Mock(status_code=200, headers={}, text='')
            response.json.return_value = payload
            return response
        client._http = Mock()
        client._http.request.side_effect = request
        tables = client.get_schemas(prior_tables=prior, progress_callback=callback)
        return [table.model_dump() for table in tables], sorted(requests)

    events = []
    plain, plain_requests = discover(None)
    reported, reported_requests = discover(lambda *e: events.append(e))
    assert reported == plain
    assert len(reported) == len(prior)
    assert reported_requests == plain_requests
    assert {e[0] for e in events} >= {'workspace_models', 'workspace_reports', 'assembling_models', 'reused_models'}
    assert any(e[1] == 'Sales' for e in events)
    assert any(e[0] == 'reused_models' and e[2:] == (1, 1) for e in events)


@pytest.mark.parametrize('kind,count', [('mssql', 10000), ('snowflake', 35000)])
def test_large_sql_discovery_preserves_metadata_and_driver_cost(kind, count, monkeypatch):
    from unittest.mock import MagicMock, patch
    from app.data_sources.clients.mssql_client import MSSQLClient
    from app.data_sources.clients.snowflake_client import SnowflakeClient
    from app.data_sources.engine_pool import ephemeral

    # Drivers return one metadata column per table, never customer row data.
    rows = [('PUBLIC', f'table_{i}', 'id', 'integer', None, None) for i in range(count)]
    monkeypatch.setattr('app.data_sources.clients.progress.time.monotonic', lambda: 10.0)
    def discover(callback, query_sink=None):
        client = (MSSQLClient('example.invalid', 1433, 'CATALOG', user='reader', password='synthetic')
                  if kind == 'mssql' else
                  SnowflakeClient('example', 'WH', 'CATALOG', user='reader', password='synthetic'))
        connection = MagicMock()
        connection.info = {'bow_spid': 7}
        queries = [] if query_sink is None else query_sink
        def execute(statement, *args, **kwargs):
            query = str(statement)
            queries.append(query)
            response = MagicMock()
            response.fetchall.return_value = rows if 'INFORMATION_SCHEMA.COLUMNS' in query else []
            return response
        connection.execute.side_effect = execute
        engine = MagicMock()
        engine.connect.return_value = connection
        inspector = MagicMock()
        inspector.get_multi_foreign_keys.return_value = {}
        with ephemeral(), patch('sqlalchemy.create_engine', return_value=engine), patch('sqlalchemy.inspect', return_value=inspector):
            tables = client.get_schemas(progress_callback=callback)
        return [table.model_dump() for table in tables], queries

    events = []
    plain, plain_queries = discover(None)
    reported, reported_queries = discover(lambda *e: events.append(e))
    assert len(reported) == count
    assert reported == plain
    assert reported_queries == plain_queries
    assert any(e[0] == 'columns' and e[2:] == (count, count) for e in events)
    assert events[-1][2:] == (count, count)
    assert len(events) < 150

    cancelled_queries = []
    def cancel(phase, item, done, total):
        if phase == 'columns' and done > 0:
            raise IndexingCancelled()
    with pytest.raises(IndexingCancelled):
        discover(cancel, cancelled_queries)
    # Cancellation must not trigger the enriched-query fallback or the next
    # metadata stage. Exactly the already-started columns query was sent.
    assert len(cancelled_queries) == 1
