"""Round-2 contracts for submit_<list>: the streaming record counter, how a
past submission is replayed to the model, and what survives decay."""
import json

from app.ai.agents.planner.planner_v3 import _RecordCounter
from app.ai.context.parts import Outcome, ToolCallPart, ToolResultPart
from app.ai.context.replay_args import compact_replayed_args
from app.ai.context.transcript import Transcript
from app.ai.agents.planner.prompt_builder import PromptBuilder
from app.ai.tools.implementations.submit_list import SubmitListTool

RECORDS = [
    {"row_id": None, "fields": {"title": {"value": 'a}{"[', "status": "found",
                                          "evidence": [{"kind": "file", "ref": "x.pdf", "page": 1, "quote": "q \\\" }"}],
                                          "note": None}}},
    {"row_id": "r2", "fields": {"title": {"value": "b", "status": "found", "evidence": [], "note": None}}},
    {"row_id": None, "fields": {}},
]


def _stream(chunk_size):
    s = json.dumps({"records": RECORDS})
    c = _RecordCounter()
    seen = [c.feed(s[i:i + chunk_size]) for i in range(0, len(s), chunk_size)]
    return seen


def test_counter_counts_finished_records_only():
    for size in (1, 3, 17, 10_000):
        seen = _stream(size)
        assert seen[-1] == 3
        assert seen == sorted(seen)  # monotone: never over-counts mid-record


def test_counter_ignores_braces_inside_strings_and_nested_objects():
    c = _RecordCounter()
    assert c.feed('{"records": [{"fields": {"a": {"value": "}}}]]", "evidence": [{"x": 1}]}}}') == 1
    assert c.feed(', {"fields": {}') == 1  # record still open
    assert c.feed('}]}') == 2


def test_successful_submission_args_are_compacted():
    args = {"list_id": "L1", "records": RECORDS}
    out = compact_replayed_args("submit_list", args, succeeded=True)
    assert out["list_id"] == "L1"
    assert isinstance(out["records"], str) and "3 record" in out["records"]
    assert args["records"] is RECORDS  # stored args untouched
    # deterministic → prompt-cache stable
    assert out == compact_replayed_args("submit_list", args, succeeded=True)


def test_failed_submission_and_other_tools_replay_verbatim():
    args = {"list_id": "L1", "records": RECORDS}
    assert compact_replayed_args("submit_list", args, succeeded=False) is args
    other = {"records": [1, 2]}
    assert compact_replayed_args("create_data", other, succeeded=True) is other


def _transcript(outcome):
    t = Transcript()
    t.add_user_text("extract")
    t.add_assistant_step(calls=[ToolCallPart(id="c1", tool_name="submit_list",
                                             args={"list_id": "L1", "records": RECORDS})])
    t.add_tool_results([ToolResultPart(call_id="c1", tool_name="submit_list", outcome=outcome, content="{}")])
    return t


def _replayed_input(t):
    msgs = t.to_model_messages()
    return next(b for m in msgs if isinstance(m.content, list) for b in m.content
                if b.get("type") == "tool_use")["input"]


def test_transcript_replays_compact_args_after_success():
    assert isinstance(_replayed_input(_transcript(Outcome.SUCCESS))["records"], str)


def test_transcript_keeps_full_args_after_failure():
    assert _replayed_input(_transcript(Outcome.FAILED))["records"] == RECORDS


def test_legacy_past_observations_compact_submit_args():
    obs = [{"tool_name": "submit_list", "tool_input": {"list_id": "L1", "records": RECORDS},
            "observation": {"success": True, "summary": "saved"}, "loop_index": 0},
           {"tool_name": "submit_list", "tool_input": {"list_id": "L1", "records": RECORDS},
            "observation": {"success": False, "summary": "rejected"}, "loop_index": 0}]
    out = PromptBuilder._compact_past_observations(obs)
    assert isinstance(out[0]["tool_input"]["records"], str)
    assert out[1]["tool_input"]["records"] == RECORDS
    assert obs[0]["tool_input"]["records"] is RECORDS


def test_digest_names_the_outcome_not_the_payload():
    keys = SubmitListTool().metadata.digest_keys
    assert {"list_name", "inserted", "updated", "unchanged", "error"} <= set(keys)
    assert "records" not in keys
