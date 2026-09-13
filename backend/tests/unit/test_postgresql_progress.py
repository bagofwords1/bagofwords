"""Schema discovery reports real work without extra database queries."""
from contextlib import contextmanager
from unittest.mock import Mock
import pytest
from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.data_sources.clients.progress import IndexingCancelled

@pytest.mark.parametrize('table_count', [0, 1, 7])
@pytest.mark.parametrize('fallback', [False, True])
def test_discovery_reports_work_without_changing_catalog_or_queries(monkeypatch, table_count, fallback):
    client = PostgresqlClient('localhost', 5432, 'demo', 'demo')
    rows = [('public', f'table_{i}', 'id', 'integer') for i in range(table_count)]
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = rows if fallback else [(*row, None, None) for row in rows]
    @contextmanager
    def connect():
        yield conn
    monkeypatch.setattr(client, 'connect', connect)
    monkeypatch.setattr(client, '_append_materialized_views', Mock())
    monkeypatch.setattr(client, '_attach_foreign_keys', Mock())
    if fallback:
        monkeypatch.setattr(client, '_get_tables_enriched', Mock(side_effect=RuntimeError('comments unavailable')))
    plain = client.get_schemas()
    query_count = conn.execute.call_count
    conn.execute.reset_mock()
    events = []
    reported = client.get_schemas(progress_callback=lambda *event: events.append(event))
    assert [table.name for table in reported] == [table.name for table in plain]
    assert conn.execute.call_count == query_count
    assert events, 'A caller must receive progress while metadata is discovered'
    assert any(phase == 'reading_columns' for phase, *_ in events)
    assert len([item for _, item, _, _ in events if item]) == table_count
    assert events[-1][2:] == (table_count, table_count)

@pytest.mark.parametrize('fallback', [False, True])
def test_cancellation_does_not_restart_discovery(monkeypatch, fallback):
    client = PostgresqlClient('localhost', 5432, 'demo', 'demo')
    connect = Mock(side_effect=AssertionError('Cancellation should precede database access'))
    monkeypatch.setattr(client, 'connect', connect)
    if fallback:
        monkeypatch.setattr(client, '_get_tables_enriched', Mock(side_effect=RuntimeError('comments unavailable')))
    def cancelled(phase, *_):
        if phase == 'reading_columns':
            raise IndexingCancelled()
    with pytest.raises(IndexingCancelled):
        client.get_schemas(progress_callback=cancelled)
    connect.assert_not_called()
