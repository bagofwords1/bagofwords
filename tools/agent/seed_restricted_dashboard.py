#!/usr/bin/env python3
"""Publish a restricted dashboard with one editor, for
restricted_dashboard_signin.mjs (docs/feedback-loops/2026-11-25-restricted-dashboard-signed-out-404.md).

First run seed_org.py with two invited members into a private JSON file:

    cd backend && uv run python ../tools/agent/seed_org.py \
        --invite editor@example.com --invite outsider@example.com > /tmp/seed.json
    uv run python ../tools/agent/seed_restricted_dashboard.py --seed /tmp/seed.json

The admin owns the dashboard; the first invited member gets "Can edit"; general
access stays "Restricted" (visibility none). Uses the sandbox's API session
only, never an LLM. Prints only the dashboard id.
"""
import argparse
import json
import time

import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--seed', required=True, help='JSON output of seed_org.py (with two --invite members)')
args = parser.parse_args()
with open(args.seed) as source:
    seed = json.load(source)

code = '''<script type="text/babel">
function App() {
  return <div className="p-8"><h1 data-testid="restricted-dashboard-body" className="text-2xl font-semibold">Restricted dashboard</h1></div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script>'''

with httpx.Client(base_url=seed['base_url'], timeout=60, headers={
    'Authorization': 'Bearer ' + seed['admin']['token'],
    'X-Organization-Id': seed['organization']['id'],
}) as client:
    def request(method, path, body=None):
        response = client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()

    report = request('POST', '/api/reports', {'title': 'Restricted dashboard verification'})
    artifact = request('POST', '/api/artifacts', {
        'report_id': report['id'], 'title': 'Restricted dashboard', 'mode': 'page',
        'content': {'code': code, 'runtime_version': 11},
    })
    artifact_id = artifact['artifact_id']
    request('POST', f'/api/artifacts/{artifact_id}/publish', {})
    for _ in range(120):
        if request('GET', f'/api/artifacts/{artifact_id}/publish').get('published'):
            break
        time.sleep(.25)
    else:
        raise RuntimeError('Sandbox dashboard did not publish')

    members = {m['email']: m['id'] for m in request('GET', '/api/organization/members')}
    editor_id = members[seed['members'][0]['email']]
    sharing = request('PUT', f'/api/artifacts/{artifact_id}/sharing',
                      {'visibility': 'none', 'editor_user_ids': [editor_id]})
    assert sharing['visibility'] == 'none' and sharing['editor_user_ids'] == [editor_id], sharing
    print(artifact_id)
