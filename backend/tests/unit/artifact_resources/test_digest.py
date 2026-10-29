from types import SimpleNamespace
import pytest
from app.ai.context.artifact_digest import digest_artifact


@pytest.mark.parametrize('wrapped', [False, True])
def test_resource_digest_retains_recovery_context_without_record_content(wrapped):
    result={'resource_artifact_id':'parent-a','id':'resource-b','name':'posts','kind':'collection',
            'action':'update','revision':4,'committed':True,'changed_sections':['permissions'],
            'definition':{'fields':{'title':{'type':'string'}},'permissions':{'read':{'audience':'public'}}},
            'records':[{'body':'private document content'}]}
    tool=SimpleNamespace(tool_name='manage_artifact_resources',result_json={'observation':result} if wrapped else result)
    text=digest_artifact(tool)
    for fact in ['parent-a','resource-b','posts','revision: 4','permissions','public','title:string','Re-read']:
        assert fact in text
    assert 'private document content' not in text


def test_failure_is_preserved_even_when_transport_succeeded():
    tool=SimpleNamespace(tool_name='manage_artifact_resources',status='success',result_json={
        'success':False,'committed':False,'error':'Revision conflict','resource_artifact_id':'parent-a'})
    text=digest_artifact(tool)
    assert 'FAILED' in text and 'Revision conflict' in text and 'committed: False' in text


def test_persisted_observation_keeps_resource_identity_under_large_definition():
    from app.ai.persisted_summary import build_tool_context_summary, tool_context_for_replay
    observation={'summary':'Updated schema','resource_artifact_id':'stable-parent',
                 'action':'update','name':'posts','revision':8,'committed':True,
                 'definition':{'fields':{f'field_{i}':{'type':'string','description':'x'*1000} for i in range(100)}}}
    stored=build_tool_context_summary('manage_artifact_resources', observation, observation=observation)
    restored=tool_context_for_replay(stored)
    assert restored['resource_artifact_id']=='stable-parent'
    assert restored['revision']==8 and restored['committed'] is True


def test_legacy_resource_observation_uses_original_arguments_for_identity():
    tool=SimpleNamespace(tool_name='manage_artifact_resources',
        arguments_json={'action':'create','artifact_id':'old-parent','definition':{'name':'notes'}},
        result_json={'resource':{'id':'old-resource','revision':2}})
    text=digest_artifact(tool)
    assert 'old-parent' in text and 'old-resource' in text and 'notes' in text and 'action: create' in text
