from app.services.artifact_conversations import timeline
from app.services.artifact_conversation_results import Redaction


def test_timeline_orders_text_and_tools_and_preserves_identity():
    blocks = [
        {'id':'end','block_index':8,'content':'Answer'},
        {'id':'tool','block_index':3,'tool_execution':{'tool_name':'execute_mcp','arguments_json':{'tool_name':'get_file_contents'},'status':'error'}},
        {'id':'start','block_index':1,'content':'Looking'},
    ]
    out = timeline(blocks, Redaction(None, True))
    assert [b['id'] for b in out] == ['start','tool','end']
    assert out[1]['title'] == 'Get file contents'
    assert out[1]['status'] == 'error'


def test_timeline_titles_fail_closed_and_prefer_authored_labels():
    blocks = [{'id':'tool','tool_execution':{'tool_name':'execute_mcp','status':'error','error_message':'Private error','arguments_json':{'title':'Private title','tool_name':'search_code'}}}]
    assert timeline(blocks, Redaction(None, False))[0]['title'] is None
    assert timeline(blocks, Redaction(lambda x:x.replace('Private','Masked'), True))[0]['title'] == 'Masked title'


def test_tool_errors_are_redacted_and_bounded():
    blocks=[{'id':'error','tool_execution':{'tool_name':'read_file','status':'error','error_message':'Secret ' * 1000}}]
    assert timeline(blocks,Redaction(None,False))[0]['detail'] is None
    shown=timeline(blocks,Redaction(lambda s:s.replace('Secret','Hidden'),True))[0]['detail']
    assert 'Secret' not in shown
    assert len(shown)<=500
