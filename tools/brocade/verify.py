#!/usr/bin/env python3
"""Run public client queries against an explicit real or synthetic endpoint.

Credentials come from BROCADE_USER/BROCADE_PASSWORD. Report contains counts
and pass/fail only, never credentials or returned customer data.
"""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from app.data_sources.clients.brocade_client import BrocadeClient


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', required=True)
    p.add_argument('--vf-id', type=int, required=True)
    p.add_argument('--allow-http', action='store_true')
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    client = BrocadeClient(a.url, os.environ['BROCADE_USER'], os.environ['BROCADE_PASSWORD'],
                           vf_ids=str(a.vf_id), allow_http=a.allow_http)
    tables = client.get_tables()
    report = {'verification': 'HTTP contract only; endpoint provenance must be recorded separately', 'primary_tables': len(tables), 'queries': []}
    for table in tables:
        query = {'table': table.name}
        if table.metadata_json['scope'] == 'logical_switch':
            query['scope'] = {'vf_id': a.vf_id}
        frame = client.execute_query(query)
        report['queries'].append({'table': table.name, 'rows': len(frame), 'columns': len(frame.columns), 'passed': set(frame.columns) == {c.name for c in table.columns}})
    if not all(q['passed'] for q in report['queries']):
        raise SystemExit('Schema verification failed')
    result = json.dumps(report, indent=2)
    if a.output:
        a.output.write_text(result + '\n')
    print(result)

if __name__ == '__main__':
    main()
