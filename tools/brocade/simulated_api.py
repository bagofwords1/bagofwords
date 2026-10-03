#!/usr/bin/env python3
"""Independent, synthetic FOS HTTP fixture server; NOT a Fabric OS emulator.

Uses documented XML resource shapes. No customer responses are included.
Only auth lifecycle POSTs and the listed diagnostic GETs are accepted.
"""
import argparse
import base64
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring


def xml(data, root='Response'):
    top = Element(root, xmlns='urn:brocade.com:mgmt')
    def add(parent, key, value):
        for item in value if isinstance(value, list) else [value]:
            e = SubElement(parent, key)
            if isinstance(item, dict):
                for k, v in item.items():
                    add(e, k, v)
            elif item is not None:
                e.text = str(item).lower() if isinstance(item, bool) else str(item)
    for key, value in data.items():
        add(top, key, value)
    return tostring(top, encoding='utf-8')


def resources(vf=10):
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(minutes=2)).isoformat()
    switch = f'10:00:00:00:00:00:00:{vf:02x}'
    return {
        'brocade-fibrechannel-switch/fibrechannel-switch': {'name': switch, 'domain-id': vf, 'vf-id': vf, 'firmware-version': 'v9.1.1c', 'user-friendly-name': f'SAN-{vf}', 'operational-status-string': 'Online'},
        'brocade-chassis/chassis': {'chassis-wwn': '10:00:00:00:00:00:ff:01', 'chassis-user-friendly-name': 'Synthetic lab'},
        'brocade-fibrechannel-logical-switch/fibrechannel-logical-switch': [{'fabric-id': 10, 'switch-wwn': '10:00:00:00:00:00:00:0a'}, {'fabric-id': 20, 'switch-wwn': '10:00:00:00:00:00:00:14'}],
        'brocade-interface/fibrechannel': [
            {'name': '0/12', 'index': 12, 'wwn': '20:00:00:00:00:00:00:12', 'operational-status-string': 'online', 'protocol-speed': '32G', 'neighbor': {'wwn': ['50:0a:09:80:12:34:56:78', '50:0a:09:80:12:34:56:79']}, 'port-type-string': 'f-port', 'port-health': 'marginal'},
            {'name': '0/24', 'index': 24, 'wwn': '20:00:00:00:00:00:00:24', 'operational-status-string': 'online', 'neighbor-switch-user-friendly-name': 'SAN-peer', 'neighbor': {'wwn': ['10:00:00:00:00:00:00:ff']}, 'port-type-string': 'e-port'}],
        'brocade-interface/fibrechannel-statistics': [
            {'name': '0/12', 'time-generated': int(now.timestamp()), 'time-refreshed': int(now.timestamp()) - 1, 'sampling-interval': 1, 'in-rate': 1500000, 'out-rate': 2500000, 'crc-errors': 5 + vf, 'in-octets': 18446744073709551614, 'class-3-discards': 7, 'link-failures': 2, 'bb-credit-zero': 40, 'total-up-time': '99.5', 'reset-statistics': False},
            {'name': '0/24', 'crc-errors': 0, 'in-rate': 1000000, 'out-rate': 2000000}],
        'brocade-media/media-rdp': {'name': '0/12', 'rx-power': '430.25', 'tx-power': '510.10', 'temperature': 42, 'vendor-name': 'Synthetic Optics', 'peer-data-available': True},
        'brocade-name-server/fibrechannel-name-server': {'port-id': 65548, 'port-name': '50:0A:09:80:12:34:56:78', 'fabric-port-name': '20:00:00:00:00:00:00:12', 'port-index': 12},
        'brocade-zone/defined-configuration': {'zone': {'zone-name': 'host_to_array', 'zone-type-string': 'peer-zone', 'member-entry': {'entry-name': ['host_alias', '2,12'], 'principal-entry-name': ['50:0a:09:80:12:34:56:78']}}, 'alias': {'alias-name': 'host_alias', 'member-entry': {'alias-entry-name': ['10:00:00:00:00:00:ab:01']}}},
        'brocade-zone/effective-configuration': {'cfg-name': 'production', 'enabled-zone': {'zone-name': 'host_to_array', 'zone-type-string': 'peer-zone', 'member-entry': {'entry-name': ['10:00:00:00:00:00:ab:01'], 'principal-entry-name': ['50:0a:09:80:12:34:56:78']}}, 'cfg-action': 1},
        'brocade-maps/switch-status-policy-report': {'switch-health': 'marginal', 'fan-health': 'healthy', 'power-supply-health': 'healthy'},
        'brocade-maps/credit-stall-dashboard': {'slot-port': '0/12', 'timestamp': stamp, 'state': 'perf-impact', 'frequency': 12},
        'brocade-maps/oversubscription-dashboard': {'slot-port': '0/24', 'timestamp': stamp, 'state': 'monitoring-paused', 'frequency': 0},
        'brocade-maps/dashboard-history': {'category': 'Port Health', 'date': now.strftime('%Y-%m-%d'), 'crc-errors': {'port-data': ['0/12:15', '0/24:0']}},
        'brocade-logging/error-log': {'sequence-number': 21, 'time-stamp': stamp, 'message-id': 'PORT-1001', 'severity-level': 'error', 'fabric-id': vf, 'message-text': 'Synthetic link integrity event', 'event-info': '0/12'},
        'brocade-logging/audit-log': {'sequence-number': 22, 'time-stamp': stamp, 'message-id': 'SEC-1001', 'severity-level': 'info', 'fabric-id': 0, 'message-text': 'Synthetic configuration audit event', 'user-name': 'lab-reader'},
        'brocade-fru/blade': {'slot-number': 1, 'blade-state': 'enabled'},
        'brocade-fru/fan': {'unit-number': 1, 'operational-state': 'ok'},
        'brocade-fru/power-supply': {'unit-number': 1, 'operational-state': 'ok'},
        'brocade-fru/sensor': {'id': 1, 'state': 'ok'},
        'brocade-fru/wwn': {'unit-number': 1},
    }


def dispatch(method, target, headers, username='lab-reader', password='synthetic-only'):
    parsed = urlsplit(target)
    path = parsed.path
    auth = headers.get('Authorization', '')
    def error(status, message):
        return status, {}, xml({'error': {'error-message': message}}, root='errors')
    if method == 'POST' and path == '/rest/login':
        token = base64.b64encode(f'{username}:{password}'.encode()).decode()
        if auth not in ('Basic ' + token, 'Custom_Basic ' + token):
            return error(401, 'Invalid credentials')
        return 200, {'Authorization': 'Custom_Basic synthetic-session'}, b''
    if auth != 'Custom_Basic synthetic-session':
        return error(401, 'Session expired')
    if method == 'POST' and path == '/rest/logout':
        return 204, {}, b''
    if method != 'GET':
        return error(405, 'Diagnostic server rejects mutations')
    params = parse_qs(parsed.query)
    if set(params) - {'vf-id'} or any(len(v) != 1 for v in params.values()):
        return error(400, 'Unexpected query parameters')
    try:
        vf = int(params.get('vf-id', ['10'])[0])
    except ValueError:
        return error(400, 'Invalid fabric')
    if vf not in (10, 20):
        return error(403, 'Fabric access denied')
    data = resources(vf)
    if path == '/rest/brocade-module-version':
        modules = {}
        for key in list(data) + ['brocade-module-version/module']:
            module, obj = key.split('/')
            modules.setdefault(module, []).append(obj)
        return 200, {}, xml({'module': [{'name': module, 'version': '1.0.0', 'objects': {'object': objs}} for module, objs in modules.items()]})
    key = path.removeprefix('/rest/running/')
    if key not in data:
        return error(404, 'Unknown resource')
    return 200, {}, xml({key.split('/')[1]: data[key]})


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond()
    def do_POST(self):
        self.respond()
    def do_PATCH(self):
        self.respond()
    def do_DELETE(self):
        self.respond()
    def respond(self):
        status, headers, body = dispatch(self.command, self.path, self.headers,
            os.environ.get('BROCADE_SIM_USER', 'lab-reader'), os.environ.get('BROCADE_SIM_PASSWORD', 'synthetic-only'))
        self.send_response(status)
        self.send_header('Content-Type', 'application/yang-data+xml')
        self.send_header('Content-Length', str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *args):
        pass  # Never print headers or credentials.


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=18092)
    args = p.parse_args()
    print(f'Synthetic FOS API on {args.host}:{args.port}', flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
