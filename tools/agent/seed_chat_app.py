#!/usr/bin/env python3
"""Publish the deterministic chat UI used by chatPublishedSmoke.mjs.

First run seed_org.py into a private JSON file. This uses that sandbox's API
session, never an LLM or production credentials. Prints only the artifact id.
"""
import argparse
import json
import time

import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--seed', required=True, help='JSON output of seed_org.py')
args = parser.parse_args()
with open(args.seed) as source:
    seed = json.load(source)

code = '''<script type="text/babel">
function App() {
  const chat = useConversation();
  return <div className="h-screen flex flex-col bg-surface p-6">
    <h1 className="text-xl font-semibold">Support</h1>
    <div className="flex-1 min-h-0"><ChatMessages chat={chat}/></div>
    <ChatComposer chat={chat} placeholder="Message support"/>
  </div>;
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

    report = request('POST', '/api/reports', {'title': 'Support app verification'})
    artifact = request('POST', '/api/artifacts', {
        'report_id': report['id'], 'title': 'Support', 'mode': 'page',
        'content': {'code': code, 'runtime_version': 11},
    })
    artifact_id = artifact['artifact_id']
    request('POST', f'/api/artifacts/{artifact_id}/publish', {})
    for _ in range(120):
        state = request('GET', f'/api/artifacts/{artifact_id}/publish')
        if state.get('published'):
            break
        time.sleep(.25)
    else:
        raise RuntimeError('Sandbox app did not publish')
    request('PUT', f'/api/artifacts/{artifact_id}/sharing', {'visibility': 'public'})
    print(artifact_id)
