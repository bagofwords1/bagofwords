"""MCP monitoring and evals follow the existing agent grants."""
import pytest

from tests.e2e.rbac.test_diagnosis_explorer_scope import world
from tests.e2e.rbac.test_rbac_eval_tools_scope import tool_world
from tests.mocks.bow_mcp_client import BowMCPClient

EVAL_TOOLS = {'search_evals', 'create_eval', 'edit_eval', 'run_eval',
              'stop_eval_run', 'get_eval_runs', 'get_eval_run'}


def client(test_client, w, actor):
    return BowMCPClient(test_client, bearer=w[actor]['token'], org_id=w['org_id'])


@pytest.mark.parametrize('actor,allowed', [('admin', True), ('manager_b', True), ('member', False)])
def test_mcp_discovers_monitoring_by_authority_in_chat(world, test_client, actor, allowed):
    c = client(test_client, world, actor)
    report = c.call('create_report', {'title': 'Monitoring from MCP'})
    assert report.ok, report.summary()
    result = c.call('get_context', {'report_id': report.payload['report_id'], 'patterns': ['bow\\.']})
    assert result.ok, result.summary()
    tables = {t['name'] for s in result.payload['data_sources'] if s['id'] == 'builtin:bow' for t in s['tables']}
    assert ('bow.runs' in tables) is allowed
    assert ('bow.tool_calls' in tables) is allowed


def test_mcp_eval_catalog_and_search_use_agent_authority(tool_world, test_client, invite_user_to_org):
    w = tool_world
    w['member'] = invite_user_to_org(org_id=w['org_id'], admin_token=w['admin']['token'])
    for actor, allowed in [('admin', True), ('mgr_a', True), ('member', False)]:
        c = client(test_client, w, actor)
        assert EVAL_TOOLS.issubset(c.tool_names()) is allowed
        result = c.call('search_evals', {'status': 'all', 'limit': 50})
        assert result.ok is allowed, result.summary()
        if actor == 'mgr_a':
            assert {item['id'] for item in result.payload['items']} == {w['case_a']}


@pytest.mark.parametrize('dataset', ['runs', 'tool_calls'])
@pytest.mark.parametrize('actor,allowed', [('admin', True), ('manager_b', True), ('member', False)])
def test_mcp_create_data_queries_only_authorized_history(
    world, test_client, monkeypatch, create_llm_provider_and_models, actor, allowed, dataset,
):
    from app.ai.llm import LLM
    from app.ai.llm.types import TextDeltaEvent

    # Stub only the LLM boundary: real context, code execution, source queries,
    # authorization, snapshot protection, and artifact persistence all run.
    monkeypatch.setenv('OPENAI_API_KEY_TEST', 'test-provider-key')
    create_llm_provider_and_models(user_token=world['admin']['token'], org_id=world['org_id'])
    request = {'dataset': dataset, 'columns': ['run_id'], 'time_range': {'relative': '7d'}}
    code = 'def generate_df(ds_clients, excel_files):\n    return ds_clients["bow"].execute_query(' + repr(request) + ')\n'

    async def scripted_llm(self, *args, **kwargs):
        yield TextDeltaEvent(text=code)
    monkeypatch.setattr(LLM, 'inference_stream_v2', scripted_llm)

    c = client(test_client, world, actor)
    report = c.call('create_report', {'title': 'History query'})
    assert report.ok, report.summary()
    result = c.call('create_data', {
        'report_id': report.payload['report_id'],
        'prompt': f'Show recent {dataset} for the last 7 days',
        'tables': [{'data_source_id': 'builtin:bow', 'tables': [f'bow.{dataset}']}],
    })
    assert result.ok is allowed, result.summary()
    if allowed:
        rows = result.payload['data_preview']['rows']
        # Preview rows are serialized by the regular data-table formatter.
        actual = {row['run_id'] for row in rows}
        expected = (set(world['ids']['b']) if actor == 'manager_b'
                    else {rid for ids in world['ids'].values() for rid in ids})
        if dataset == 'tool_calls':
            expected -= {world['ids']['b'][1]}  # this successful run made no calls
        assert actual == expected
        assert result.payload['query_id']
        assert result.payload['visualization_id']


def test_mcp_eval_authoring_preserves_all_target_authority(tool_world, test_client):
    w = tool_world
    c = client(test_client, w, 'mgr_a')
    for targets, allowed in [([w['ds_a']['id']], True), ([w['ds_b']['id']], False),
                             ([w['ds_a']['id'], w['ds_b']['id']], False), ([], False)]:
        result = c.call('create_eval', {
            'name': 'MCP permission contract', 'prompt': {'content': 'List recent orders'},
            'expectations': {'rules': []}, 'data_source_ids': targets,
        })
        assert result.ok is allowed, result.summary()
        if allowed:
            case_id = result.payload['case_id']
            edited = c.call('edit_eval', {'case_id': case_id, 'name': 'Updated MCP case'})
            assert edited.ok, edited.summary()
    for key in ('case_b', 'case_ab', 'case_global'):
        edited = c.call('edit_eval', {'case_id': w[key], 'name': 'Unauthorized edit'})
        assert edited.refused, edited.summary()
        run = c.call('run_eval', {'case_ids': [w[key]]})
        assert run.refused, run.summary()
    history = c.call('get_eval_runs', {})
    assert history.ok, history.summary()


def test_mcp_eval_runs_preserve_read_stop_and_dedup_authority(
    tool_world, test_client, monkeypatch, create_llm_provider_and_models,
):
    w = tool_world
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "test-provider-key")
    create_llm_provider_and_models(user_token=w["admin"]["token"], org_id=w["org_id"])
    admin_headers = {'Authorization': f"Bearer {w['admin']['token']}", 'X-Organization-Id': w['org_id']}
    runs = {}
    for key in ('case_a', 'case_b', 'case_ab', 'case_global'):
        # The REST API creates pending runs without opening an execution stream.
        # This also exercises MCP run_eval's existing-run deduplication contract.
        created = test_client.post('/api/tests/runs', json={'case_ids': [w[key]]}, headers=admin_headers)
        assert created.status_code == 200, created.text
        runs[key] = created.json()['id']
    c = client(test_client, w, 'mgr_a')
    listed = c.call('get_eval_runs', {'limit': 50})
    assert listed.ok, listed.summary()
    assert {run['run_id'] for run in listed.payload['items']} == {runs['case_a']}
    for key, run_id in runs.items():
        allowed = key == 'case_a'
        detail = c.call('get_eval_run', {'run_id': run_id, 'include_transcript': True})
        assert detail.ok is allowed, detail.summary()
        if allowed:
            started = c.call('run_eval', {'case_ids': [w[key]], 'wait_s': 0})
            assert started.ok, started.summary()
            assert started.payload['run_id'] == run_id
            assert started.payload['detached']
        stopped = c.call('stop_eval_run', {'run_id': run_id})
        assert stopped.ok is allowed, stopped.summary()
    detail = c.call('get_eval_run', {'run_id': runs['case_a']})
    assert detail.ok, detail.summary()
    assert detail.payload['status'] == 'stopped'
