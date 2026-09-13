"""Public connector invariants, with HTTP as the only mocked boundary."""
import importlib.util
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import pytest
import requests
from app.schemas.data_source_registry import resolve_client_class
from app.data_sources.clients.brocade_client import BrocadeClient, BrocadeError

_spec = importlib.util.spec_from_file_location('brocade_sim', Path(__file__).resolve().parents[3] / 'tools/brocade/simulated_api.py')
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)
PRIMARY = ['switches', 'ports', 'port_statistics', 'transceivers', 'connected_devices', 'fabric_links', 'zones', 'zone_members', 'health', 'congestion_samples', 'events', 'hardware']

@pytest.fixture
def boundary(monkeypatch):
    calls, overrides = [], {}
    def request(session, method, url, **kwargs):
        path = urlsplit(url).path
        calls.append((method, path, kwargs.get('params')))
        target = path + ('?' + urlencode(kwargs['params']) if kwargs.get('params') else '')
        result = overrides.get((method, path)) or sim.dispatch(method, target, session.headers)
        response = requests.Response()
        response.status_code, response.headers, response._content = result
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, 'request', request)
    return calls, overrides

@pytest.fixture
def client(boundary):
    return BrocadeClient('https://switch.example', 'lab-reader', 'synthetic-only', vf_ids='10,20')


def test_brocade_registry_resolves_query_client():
    cls = resolve_client_class('brocade')
    assert callable(cls.get_schemas) and callable(cls.execute_query)

@pytest.mark.parametrize('table', PRIMARY)
def test_primary_tables_return_all_reviewed_columns(client, table):
    schema = client.get_schema(table)
    query = {'table': table}
    if schema.metadata_json['scope'] == 'logical_switch':
        query['scope'] = {'vf_id': 10}
    frame = client.execute_query(query)
    assert not frame.empty
    assert set(frame.columns) == {c.name for c in schema.columns}
    assert frame['_bow_complete'].all()
    assert frame['_bow_source_resource'].notna().all()
    assert not any('password' in c or 'reset_statistics' in c or 'cfg_action' in c for c in frame.columns)


def test_primary_and_advanced_catalogs_are_distinct(client):
    assert {t.name for t in client.get_tables()} == set(PRIMARY)
    advanced = client.get_tables(include_advanced=True)
    assert len(advanced) > len(PRIMARY)
    assert any(t.metadata_json['availability'] == 'unsupported' for t in advanced)


def test_all_fields_does_not_fetch_unrelated_resources(client, boundary):
    frame = client.execute_query(table='port_statistics', scope={'vf_id': 10}, filter={'port': '0/12'})
    assert frame.iloc[0]['in_octets'] == 18446744073709551614
    assert frame.iloc[0]['receive_bytes_per_second'] == 1500000
    assert frame.iloc[0]['total_up_time'] == 99.5
    assert not any('/media-rdp' in path or '/brocade-zone/' in path for _, path, _ in boundary[0])
    assert all(method == 'GET' or path in ('/rest/login', '/rest/logout') for method, path, _ in boundary[0])


def test_projection_preserves_evidence_and_scope(client):
    frame = client.execute_query(table='ports', scope={'vf_id': 20}, fields=['port', 'operational_status'])
    assert set(c for c in frame.columns if not c.startswith('_bow_')) == {'port', 'operational_status'}
    assert set(frame['_bow_vf_id']) == {20}
    assert set(frame['_bow_switch_wwn']) == {'10:00:00:00:00:00:00:14'}


def test_zone_members_preserve_state_and_principal_role(client):
    frame = client.execute_query(table='zone_members', scope={'vf_id': 10})
    assert {'defined', 'effective'} == set(frame['configuration_state'])
    assert {'principal', 'member'} == set(frame['member_role'])
    assert {'wwpn', 'alias', 'domain_index'} <= set(frame['member_kind'])


def test_event_discriminator_selects_constituent(client, boundary):
    frame = client.execute_query(table='events', scope={'vf_id': 10}, filter={'event_source': 'raslog'}, lookback='1h')
    assert set(frame['event_source']) == {'raslog'}
    assert not any(path.endswith('/audit-log') for _, path, _ in boundary[0])


def test_events_preserve_chassis_context_and_retention_limits(client):
    frame = client.execute_query(table='events', scope={'vf_id': 10}, lookback='24h')
    assert 0 in set(frame['fabric_id'])
    assert set(frame['_bow_history_coverage']) == {'retention_unknown'}


def test_advanced_fields_preserve_units(client):
    schema = client.get_schema('diag_brocade_media__media_rdp')
    assert next(c for c in schema.columns if c.name == 'rx-power').metadata['units'] == 'uW'
    assert client.execute_query(table=schema.name, scope={'vf_id': 10}).iloc[0]['rx-power'] == 430.25

@pytest.mark.parametrize('query', [
    {'table': 'ports'}, {'table': 'ports', 'scope': {'vf_id': True}},
    {'table': 'ports', 'scope': {'vf_id': 129}},
    {'table': 'ports', 'scope': {'vf_id': 10}, 'method': 'PATCH'},
    {'table': 'ports', 'scope': {'vf_id': 10}, 'fields': ['reset_statistics']},
    {'table': 'ports', 'scope': {'vf_id': 10}, 'fields': []},
    {'table': 'ports', 'scope': {'vf_id': 10}, 'filter': {'port': {'contains': '0'}}},
    {'table': 'ports', 'scope': {'vf_id': 10}, 'filter': {'port': 12}},
    {'table': 'ports', 'scope': {'vf_id': 10}, 'limit': True},
    {'table': 'ports', 'scope': {'vf_id': 10}, 'lookback': '24h'},
    {'table': 'events', 'scope': {'vf_id': 10}, 'start_time': '2026-01-01', 'end_time': '2026-01-02'},
    {'table': 'events', 'scope': {'vf_id': 10}, 'lookback': '1h', 'end_time': '2026-01-01T00:00:00Z'},
    {'table': 'hardware', 'scope': {'vf_id': 10}}, {'table': 'http://evil.example/rest/password'},
])
def test_invalid_queries_rejected_before_network(client, boundary, query):
    with pytest.raises(BrocadeError):
        client.execute_query(query)
    assert not boundary[0]


def test_connection_scope_enforced_before_network(client, boundary):
    with pytest.raises(BrocadeError) as error:
        client.execute_query(table='ports', scope={'vf_id': 30})
    assert error.value.code == 'PermissionDenied'
    assert not boundary[0]

@pytest.mark.parametrize('status,code', [(401, 'AuthenticationFailed'), (403, 'PermissionDenied'), (404, 'UnsupportedResource'), (405, 'UnsupportedResource')])
def test_remote_errors_are_not_empty_results(client, boundary, status, code):
    boundary[1][('GET', '/rest/running/brocade-interface/fibrechannel')] = (status, {}, sim.xml({'error': {'error-message': 'denied'}}, 'errors'))
    with pytest.raises(BrocadeError) as error:
        client.execute_query(table='ports', scope={'vf_id': 10})
    assert error.value.code == code
    assert boundary[0][-1][1] == '/rest/logout'


def test_known_empty_collection_keeps_schema(client, boundary):
    boundary[1][('GET', '/rest/running/brocade-name-server/fibrechannel-name-server')] = (404, {}, sim.xml({'error': {'error-message': 'No entries in Name Server'}}, 'errors'))
    frame = client.execute_query(table='connected_devices', scope={'vf_id': 10})
    assert frame.empty
    assert {'wwpn', '_bow_source_resource'} <= set(frame.columns)


def test_result_limit_never_silently_truncates(client):
    with pytest.raises(BrocadeError) as error:
        client.execute_query(table='ports', scope={'vf_id': 10}, limit=1)
    assert error.value.code == 'ResultLimitExceeded'


def test_xml_entity_expansion_rejected(client, boundary):
    boundary[1][('GET', '/rest/running/brocade-interface/fibrechannel')] = (200, {}, b'<!DOCTYPE x [<!ENTITY a "expanded">]><Response><fibrechannel><name>&a;</name></fibrechannel></Response>')
    with pytest.raises(BrocadeError) as error:
        client.execute_query(table='ports', scope={'vf_id': 10})
    assert error.value.code == 'InvalidResponse'


def test_undeclared_fields_do_not_leak(client, boundary):
    boundary[1][('GET', '/rest/running/brocade-interface/fibrechannel')] = (200, {}, sim.xml({'fibrechannel': {'name': '0/1', 'new-secret': 'do-not-return'}}))
    frame = client.execute_query(table='ports', scope={'vf_id': 10})
    assert 'new-secret' not in frame.columns
    assert 'do-not-return' not in frame.to_json()

@pytest.mark.parametrize('url', ['http://switch.example', 'https://user:pass@switch.example', 'https://switch.example/rest', 'https://switch.example?url=evil'])
def test_invalid_origins_rejected(url):
    with pytest.raises(BrocadeError):
        BrocadeClient(url, 'u', 'p', allow_http=True)


def test_json_string_and_sql_argument_follow_same_contract(client):
    query = {'table': 'ports', 'scope': {'vf_id': 10}, 'filter': {'port': '0/12'}}
    assert client.execute_query(sql=json.dumps(query)).iloc[0]['port'] == '0/12'


def test_projection_keeps_entity_and_observation_times(client):
    frame = client.execute_query(table='port_statistics', scope={'vf_id': 10}, fields=['crc_errors'])
    assert {'port', 'sample_time', 'query_time', 'crc_errors'} <= set(frame.columns)


def test_wwpn_filter_is_case_insensitive(client):
    frame = client.execute_query(table='connected_devices', scope={'vf_id': 10})
    wwpn = frame.iloc[0]['wwpn']
    assert len(client.execute_query(table='connected_devices', scope={'vf_id': 10}, keys={'wwpn': wwpn.upper()})) == 1


@pytest.mark.parametrize('keys', [{'crc_errors': 5}, {'port': {'eq': '0/12'}}])
def test_keys_are_entity_equality_only(client, boundary, keys):
    with pytest.raises(BrocadeError, match='InvalidQuery'):
        client.execute_query(table='port_statistics', scope={'vf_id': 10}, keys=keys)
    assert not boundary[0]


@pytest.mark.parametrize('status,code', [(401, 'AuthenticationFailed'), (403, 'PermissionDenied'), (302, 'RemoteError')])
def test_non_xml_errors_keep_meaning(client, boundary, status, code):
    boundary[1][('POST', '/rest/login')] = (status, {}, b'<html>Unavailable')
    with pytest.raises(BrocadeError, match=code):
        client.get_tables()


def test_nested_leaf_lists_preserve_grouping(client, boundary):
    path = '/rest/running/brocade-zone/defined-configuration'
    boundary[1][('GET', path)] = (200, {}, b'<Response><defined-configuration><zone><zone-name>a</zone-name><member-entry><entry-name>x</entry-name><entry-name>y</entry-name></member-entry></zone><zone><zone-name>b</zone-name><member-entry><entry-name>z</entry-name></member-entry></zone></defined-configuration></Response>')
    frame = client.execute_query(table='diag_brocade_zone__defined_configuration', scope={'vf_id': 10})
    assert frame.iloc[0]['zone.member-entry.entry-name'] == [['x', 'y'], 'z']


def test_progress_reports_completed_catalog(client):
    reports = []
    assert len(client.get_schemas(lambda *args: reports.append(args))) == 12
    assert reports[-1][2:] == (12, 12)


def test_fabric_links_include_unnamed_or_down_isl(client, boundary):
    boundary[1][('GET', '/rest/running/brocade-interface/fibrechannel')] = (200, {}, sim.xml({'fibrechannel': [
        {'name': '0/7', 'port-type-string': 'e-port', 'operational-status-string': 'offline'},
        {'name': '0/9', 'port-type-string': 'f-port'}]}))
    frame = client.execute_query(table='fabric_links', scope={'vf_id': 10})
    assert list(frame['port']) == ['0/7']


def test_application_executor_preserves_json_query_and_uint64(client):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor
    code = 'def generate_df(db_clients, excel_files):\n    return db_clients["san"].execute_query({"table":"port_statistics","scope":{"vf_id":10},"filter":{"port":"0/12"}})'
    frame, _, _ = StreamingCodeExecutor().execute_code(code=code, ds_clients={'san': client}, excel_files=[])
    assert frame.iloc[0]['in_octets'] == 18446744073709551614
    assert frame.iloc[0]['_bow_complete'] is True


def test_byte_budget_rejects_large_response(client, boundary):
    client.max_response_bytes = 1024
    boundary[1][('GET', '/rest/running/brocade-interface/fibrechannel')] = (200, {}, b'x' * 1025)
    with pytest.raises(BrocadeError, match='ResponseLimitExceeded'):
        client.execute_query(table='ports', scope={'vf_id': 10})


def test_offset_aware_event_ordering_uses_instants(client, boundary):
    boundary[1][('GET', '/rest/running/brocade-logging/error-log')] = (200, {}, sim.xml({'error-log': [
        {'sequence-number': 1, 'time-stamp': '2026-01-01T03:00:00+03:00'},
        {'sequence-number': 2, 'time-stamp': '2026-01-01T01:00:00+00:00'}]}))
    frame = client.execute_query(table='events', scope={'vf_id': 10}, filter={'event_source': 'raslog'}, order_by=[{'field': 'event_time', 'direction': 'asc'}])
    assert list(frame['sequence_number']) == [1, 2]


def test_records_without_identity_fail_instead_of_appearing_complete(client, boundary):
    boundary[1][('GET', '/rest/running/brocade-interface/fibrechannel')] = (200, {}, sim.xml({'fibrechannel': {'operational-status-string': 'online'}}))
    with pytest.raises(BrocadeError, match='InvalidResponse'):
        client.execute_query(table='ports', scope={'vf_id': 10})


def test_hardware_union_preserves_fractional_temperatures(client, boundary):
    boundary[1][('GET', '/rest/running/brocade-fru/power-supply')] = (200, {}, sim.xml({'power-supply': {'unit-number': 1, 'temperature': '42.5'}}))
    schema = client.get_schema('hardware')
    assert next(c.dtype for c in schema.columns if c.name == 'temperature') == 'number'
    frame = client.execute_query(table='hardware', filter={'temperature': {'gt': 42.1}})
    assert len(frame) == 1 and frame.iloc[0]['temperature'] == 42.5
