"""Reviewed primary views over the bundled Brocade YANG baseline."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def resource_catalog():
    return json.loads((Path(__file__).parent / 'brocade/catalog.json').read_text())


# Parts are (resource, nested row selector, discriminators). No dynamic URLs.
PRIMARY = {
    'switches': [('brocade-fibrechannel-switch/fibrechannel-switch', '', {})],
    'ports': [('brocade-interface/fibrechannel', '', {})],
    'port_statistics': [('brocade-interface/fibrechannel-statistics', '', {})],
    'transceivers': [('brocade-media/media-rdp', '', {})],
    'connected_devices': [('brocade-name-server/fibrechannel-name-server', '', {})],
    'fabric_links': [('brocade-interface/fibrechannel', '', {})],
    'zones': [('brocade-zone/defined-configuration', 'zone', {'configuration_state': 'defined'}),
              ('brocade-zone/effective-configuration', 'enabled-zone', {'configuration_state': 'effective'})],
    'zone_members': [('brocade-zone/defined-configuration', 'zone', {'configuration_state': 'defined'}),
                     ('brocade-zone/effective-configuration', 'enabled-zone', {'configuration_state': 'effective'})],
    'health': [('brocade-maps/switch-status-policy-report', '', {})],
    'congestion_samples': [('brocade-maps/credit-stall-dashboard', '', {'observation_type': 'credit_stall'}),
                           ('brocade-maps/oversubscription-dashboard', '', {'observation_type': 'oversubscription'})],
    'events': [('brocade-logging/error-log', '', {'event_source': 'raslog'}),
               ('brocade-logging/audit-log', '', {'event_source': 'audit'})],
    'hardware': [('brocade-fru/' + kind, '', {'component_type': kind.replace('-', '_')})
                 for kind in ('blade', 'fan', 'power-supply', 'sensor', 'wwn')],
}
PURPOSE = {
    'switches': 'Logical switch identity, firmware and fabric context.',
    'ports': 'Current FC port state and configuration; neighbors can be one-to-many.',
    'port_statistics': 'Current port observations: rates and cumulative counters, not arbitrary history.',
    'transceivers': 'Local and available peer optical readings; power is microwatts.',
    'connected_devices': 'Observed FC registrations and attachments; WWPNs identify endpoints.',
    'fabric_links': 'E/EX ports and observed switch neighbors, including down configured links; does not infer routes.',
    'zones': 'Zone per defined/effective state. Defined configuration does not prove active access.',
    'zone_members': 'Zone membership preserving member syntax and peer-zone principal roles.',
    'health': 'Component health assessments from MAPS.',
    'congestion_samples': 'Retained port/minute observations; monitoring-paused is not no-congestion.',
    'events': 'Retained RASLog/audit events. Complete retrieval does not guarantee requested history retention.',
    'hardware': 'Component observations; component_type distinguishes different hardware grains.',
}
ALIASES = {
    'port_statistics': {'name': 'port', 'time-generated': 'query_time', 'time-refreshed': 'sample_time',
        'in-rate': 'receive_bytes_per_second', 'out-rate': 'transmit_bytes_per_second',
        'class-3-discards': 'class3_discards'},
    'ports': {'name': 'port', 'operational-status-string': 'operational_status',
        'operational-status': 'operational_status_code', 'neighbor.wwn': 'neighbor_wwpn'},
    'transceivers': {'name': 'port', 'rx-power': 'rx_power_uw', 'tx-power': 'tx_power_uw', 'temperature': 'temperature_c'},
    'connected_devices': {'port-name': 'wwpn', 'fabric-port-name': 'fabric_port_wwpn'},
    'congestion_samples': {'slot-port': 'port', 'timestamp': 'sample_time'},
    'events': {'time-stamp': 'event_time', 'severity-level': 'severity'},
}
ALIASES['fabric_links'] = ALIASES['ports']
TIME_FIELDS = {'events': 'event_time', 'congestion_samples': 'sample_time'}
EVIDENCE = {
    '_bow_query_id': 'string', '_bow_source_url': 'string', '_bow_switch_wwn': 'string',
    '_bow_vf_id': 'integer', '_bow_retrieved_at': 'string', '_bow_source_resource': 'string',
    '_bow_firmware': 'string', '_bow_history_coverage': 'string', '_bow_complete': 'boolean',
}


def alias(table, path):
    return ALIASES.get(table, {}).get(path, path.replace('-', '_'))


def definitions():
    resources = resource_catalog()['resources']
    out = {}
    for name, parts in PRIMARY.items():
        cols = {}
        for resource, selector, tags in parts:
            for f in resources[resource]['fields']:
                path = f['path']
                if selector:
                    if not path.startswith(selector + '.'):
                        continue
                    path = path[len(selector) + 1:]
                if name in ('zones', 'zone_members') and path.startswith('member-entry.'):
                    continue
                col = alias(name, path)
                dtype = f['item_type'] if selector else f['dtype']
                if col in cols and {cols[col]['dtype'], dtype} <= {'integer', 'number'}:
                    dtype = 'number' if 'number' in (cols[col]['dtype'], dtype) else 'integer'
                cols[col] = dict(f, path=path, dtype=dtype)
            for key in tags:
                cols[key] = {'dtype': 'string', 'path': None}
        if name == 'zone_members':
            cols.update({k: {'dtype': 'string', 'path': None} for k in ('member', 'member_role', 'member_kind')})
        if name == 'health':
            cols = {'component': {'dtype': 'string'}, 'status': {'dtype': 'string'}}
        out[name] = {'parts': parts, 'columns': cols, 'primary': True, 'description': PURPOSE[name],
                     'scope': resources[parts[0][0]]['scope'], 'time_field': TIME_FIELDS.get(name)}
    for key, r in resources.items():
        name = 'diag_' + key.replace('/', '__').replace('-', '_')
        cols = {f['path']: dict(f) for f in r['fields']}
        time_field = next((p for p in ('time-stamp', 'timestamp') if p in cols), None)
        out[name] = {'parts': [(key, '', {})], 'columns': cols, 'primary': False,
                     'description': 'Diagnostic read: ' + key + '. Baseline 9.1.0b; target 9.1.1c requires live verification.',
                     'scope': r['scope'], 'time_field': time_field}
    primary_identity = {
        'switches': ['name'], 'ports': ['port'], 'port_statistics': ['port'],
        'transceivers': ['port'], 'connected_devices': ['wwpn'], 'fabric_links': ['port'],
        'zones': ['zone_name', 'configuration_state'],
        'zone_members': ['zone_name', 'configuration_state', 'member', 'member_role'],
        'health': ['component'], 'congestion_samples': ['port', 'observation_type'],
        'events': ['event_source', 'sequence_number'], 'hardware': ['component_type', 'slot_number', 'unit_number', 'id'],
    }
    for name, d in out.items():
        native_keys = resources[d['parts'][0][0]]['keys']
        d['identity_fields'] = [k for k in primary_identity.get(name, native_keys) if k in d['columns']]
        d['timestamp_fields'] = [k for k in d['columns'] if k in ('event_time', 'sample_time', 'query_time', 'time-stamp', 'timestamp', 'time-generated', 'time-refreshed')]
        d['columns'].update({k: {'dtype': v, 'path': None} for k, v in EVIDENCE.items()})
    return out
