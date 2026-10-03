#!/usr/bin/env python3
"""Compile reviewed readable FOS resources from vendor YANG (build-time pyang).

Usage: python generate_catalog.py /path/to/yang/9.1.0/9.1.0b
Sources: https://github.com/brocade/yang/tree/master/9.1.0/9.1.0b
Generated metadata is a baseline, not a claim of 9.1.1c hardware verification.
"""
import argparse
import hashlib
import json
from pathlib import Path
from pyang import context, repository

# Explicit resource exclusions; configuration reads otherwise remain useful RCA.
EXCLUDED = {
    'brocade-security': {'user-specific-password-cfg', 'password-cfg', 'user-config',
        'radius-server', 'tacacs-server', 'ldap-server', 'sec-crypto-cfg-template-action',
        'sshutil', 'sshutil-key', 'sshutil-public-key-action', 'password',
        'security-certificate-generate', 'security-certificate-action',
        'dh-chap-authentication-secret'},
    'brocade-snmp': {'v1-account', 'v3-account'},
    'brocade-time': {'ntp-clock-server-key'},
    'brocade-logging': {'supportftp'},
    'brocade-supportlink': {'supportlink-profile'},
    'brocade-usb': {'usb-file'},
    'brocade-license': {'end-user-license-agreement'},
}
DENIED_LEAVES = {'reset-statistics', 'clear-data', 'cfg-action', 'transaction-token',
    'lock-principal-transaction-token', 'test-email', 'password', 'secret',
    'shared-secret', 'community-name', 'community-string', 'auth-password',
    'priv-password', 'authentication-secret', 'private-key', 'key-string', 'clear-log'}
CHASSIS_MODULES = {'brocade-chassis', 'brocade-fru', 'brocade-firmware',
    'brocade-license', 'brocade-module-version', 'brocade-time',
    'brocade-management-ip-interface', 'brocade-supportlink', 'brocade-usb'}


def arg(s, name):
    c = s.search_one(name)
    return c.arg if c is not None else None


def type_info(s):
    t = s.search_one('type')
    units, enums = arg(s, 'units'), []
    seen = set()
    while t is not None:
        enums += [e.arg for e in t.search('enum')]
        td = getattr(t, 'i_typedef', None)
        if td is None or id(td) in seen:
            break
        seen.add(id(td))
        units = units or arg(td, 'units')
        t = td.search_one('type')
    native = t.arg if t is not None else 'string'
    dtype = ('integer' if native.startswith(('uint', 'int')) else
             'number' if native == 'decimal64' else
             'boolean' if native in ('boolean', 'empty') else 'string')
    return dtype, units, enums, native


def fields(s, prefix='', repeated=False):
    result = []
    for c in getattr(s, 'i_children', []):
        if c.arg in DENIED_LEAVES or any(w in c.arg for w in ('password', 'secret', 'private-key')):
            continue
        if c.keyword in ('choice', 'case'):
            result += fields(c, prefix, repeated)
        elif c.keyword in ('leaf', 'leaf-list'):
            if arg(c, 'status') == 'obsolete':
                continue
            dtype, units, enums, native = type_info(c)
            path = prefix + c.arg
            kind = 'array' if repeated or c.keyword == 'leaf-list' else dtype
            result.append({'path': path, 'dtype': kind, 'item_type': dtype,
                           'units': units, 'enum': enums, 'native_type': native})
        else:
            result += fields(c, prefix + c.arg + '.', repeated or c.keyword == 'list')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('yang_dir', type=Path)
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[2] /
                   'backend/app/data_sources/clients/brocade/catalog.json')
    a = p.parse_args()
    ctx = context.Context(repository.FileRepository(str(a.yang_dir)))
    files = sorted(a.yang_dir.glob('*.yang'))
    mods = [ctx.add_module(f.name, f.read_text()) for f in files]
    ctx.validate()
    resources, dispositions = {}, []
    for m in mods:
        for c in getattr(m, 'i_children', []):
            if c.keyword == 'rpc':
                dispositions.append({'resource': m.arg + '/' + c.arg, 'decision': 'excluded_rpc'})
                continue
            if c.keyword != 'container':
                continue
            for r in getattr(c, 'i_children', []):
                if r.keyword not in ('list', 'container'):
                    continue
                key = m.arg + '/' + r.arg
                excluded = r.arg in EXCLUDED.get(m.arg, set())
                dispositions.append({'resource': key, 'decision': 'excluded_sensitive_or_action' if excluded else 'conditional_read'})
                if excluded:
                    continue
                scope = 'chassis' if m.arg in CHASSIS_MODULES or m.arg == 'brocade-fibrechannel-logical-switch' else 'logical_switch'
                resources[key] = {'uri': '/rest/running/' + key,
                    'module': m.arg, 'object': r.arg, 'kind': r.keyword,
                    'scope': scope, 'keys': (arg(r, 'key') or '').split(),
                    'fields': fields(r), 'source': 'https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/' + m.arg + '.yang'}
    resources['brocade-module-version/module']['uri'] = '/rest/brocade-module-version'
    output = {'baseline': '9.1.0b', 'target': '9.1.1c', 'live_verified': False,
              'upstream_validation_diagnostics': len(ctx.errors),
              'source_sha256': {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files},
              'resources': resources, 'dispositions': dispositions}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(output, indent=2, sort_keys=True) + '\n')
    print(f'{len(resources)} conditional read resources, {len(dispositions)} dispositions')


if __name__ == '__main__':
    main()
