"""Synthetic client-to-ASGI contract checks. No customer fixtures or credentials.

Response shapes follow the public vmware/vcf-api-specs Operations OpenAPI.
The simulator remains an external boundary, not evidence of appliance compatibility.
"""
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app.data_sources.clients.aria_operations_client import AriaOperationsClient, _CATALOG


@pytest.fixture
def appliance(monkeypatch):
    path = Path(__file__).resolve().parents[3] / 'tools/aria_operations/mock_suite_api.py'
    spec = importlib.util.spec_from_file_location('synthetic_aria_mock', path)
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    with TestClient(mock.app) as http:
        class Session:
            def request(self, method, url, **kwargs):
                kwargs.pop('verify', None)
                kwargs.pop('timeout', None)
                return http.request(method, url, **kwargs)
            def post(self, url, **kwargs):
                return self.request('POST', url, **kwargs)
        monkeypatch.setattr('app.data_sources.clients.aria_operations_client.requests.Session', Session)
        yield mock, http


def test_every_catalog_table_and_wide_metrics(appliance):
    mock, _ = appliance
    client = AriaOperationsClient(url='http://testserver', username='admin', password='Aria!2024')
    assert client.test_connection()["success"]
    tables = client.get_schemas()
    assert set(_CATALOG) <= {t.name for t in tables}
    scope = {'adapter_kind': 'VMWARE', 'resource_kind': 'VirtualMachine'}
    window = {'start_time': mock.T0 - 600000, 'end_time': mock.T_END + 600000}
    specs = {
        'adapter_kinds': {}, 'resource_kinds': {}, 'stat_keys': scope, 'resources': scope,
        'properties': scope, 'relationships': scope,
        'metrics': {**scope, **window, 'stat_key': 'cpu|usage_average', 'dt': True},
        'metrics_latest': {**scope, 'stat_key': 'cpu|usage_average'},
        'metrics_topn': {**scope, **window, 'stat_key': 'cpu|usage_average'},
        'alerts': window, 'symptoms': window, 'alert_definitions': {}, 'custom_groups': {},
        'contributing_symptoms': {'alert_id': [a['alertId'] for a in mock.ALERTS]},
    }
    for table, options in specs.items():
        df = client.execute_query({'table': table, **options})
        assert not df.empty, table
        assert set(df.columns) == {c for c, _ in _CATALOG[table]['columns']}, table
    wide = client.execute_query({'table': 'metrics::VMWARE/VirtualMachine', **window, 'stat_key': 'cpu|usage_average'})
    assert not wide.empty and 'cpu|usage_average' in wide
    # Server-side invalidation exercises refresh-on-401 through the real HTTP boundary.
    mock._tokens.clear()
    assert not client.execute_query({'table': 'resources', **scope}).empty


def test_mock_uses_official_topn_and_symptom_time_shapes(appliance):
    mock, http = appliance
    token = http.post('/suite-api/api/auth/token/acquire', json={
        'username': 'admin', 'password': 'Aria!2024', 'authSource': 'LOCAL'}).json()['token']
    headers = {'Authorization': f'OpsToken {token}', 'Accept': 'application/json'}
    result = http.get('/suite-api/api/resources/stats/topn', headers=headers, params={
        'resourceId': [r['identifier'] for r in mock.RESOURCES], 'statKey': 'cpu|usage_average', 'begin': mock.T0, 'end': mock.T_END, 'topN': 2}).json()
    groups = result['resourceStatGroups']
    assert groups
    for group in groups:
        for row in group['resourceStats']:
            assert isinstance(row['stat'], dict)
            assert 'stat-list' not in row
    result = http.post('/suite-api/api/symptoms/query', headers=headers, json={
        'startTimeRange': {'startTime': mock.T_END + 1, 'endTime': mock.T_END + 2}}).json()
    assert result['symptom'] == []
