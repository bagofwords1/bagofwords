"""Unit tests for CheckinService scheduling against a stubbed scheduler
(same stub pattern as tests/unit/test_wait_tool.py).

  - arm registers a one-shot 'date' job with id checkin:<id> and only the id
    in kwargs (everything else is re-read at fire time)
  - _remove_job refuses non-checkin ids and is idempotent
  - run_checkin_wake is a module-level callable that survives jobstore
    serialization (obj_to_ref/ref_to_obj)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from apscheduler.jobstores.base import JobLookupError

import app.services.checkin_service as cs


def _row(due=datetime(2026, 10, 6, 10, 30)):
    return SimpleNamespace(id=str(uuid.uuid4()), report_id="r-1", due_at=due)


def test_arm_registers_one_shot_date_job():
    svc = cs.CheckinService()
    row = _row()
    with patch.object(cs, "scheduler") as sched:
        job_id = svc.arm(row)
    assert job_id == f"checkin:{row.id}"
    kwargs = sched.add_job.call_args.kwargs
    assert kwargs["trigger"] == "date"
    assert kwargs["id"] == job_id
    assert kwargs["func"] is cs.run_checkin_wake
    assert kwargs["run_date"] == row.due_at.replace(tzinfo=timezone.utc)
    assert kwargs["kwargs"] == {"checkin_id": row.id}  # id only
    assert kwargs["replace_existing"] is True
    assert kwargs["misfire_grace_time"] >= 3600


def test_remove_job_only_touches_checkin_ids():
    svc = cs.CheckinService()
    with patch.object(cs, "scheduler") as sched:
        assert svc._remove_job("wait:r:abc") is False
        assert svc._remove_job(None) is False
        sched.remove_job.assert_not_called()
        assert svc._remove_job("checkin:123") is True
        sched.remove_job.assert_called_once_with(job_id="checkin:123")


def test_remove_job_is_idempotent_when_already_gone():
    svc = cs.CheckinService()
    with patch.object(cs, "scheduler") as sched:
        sched.remove_job.side_effect = JobLookupError("checkin:gone")
        assert svc._remove_job("checkin:gone") is False


def test_wake_callable_round_trips_through_jobstore_refs():
    from apscheduler.util import obj_to_ref, ref_to_obj

    ref = obj_to_ref(cs.run_checkin_wake)
    assert ref == "app.services.checkin_service:run_checkin_wake"
    assert ref_to_obj(ref) is cs.run_checkin_wake


def test_small_model_call_can_record_usage_from_worker_thread(monkeypatch):
    """The planner/judge call LLM.inference in a worker thread; usage is
    scheduled onto the main loop. In a fresh process (a scheduler fire right
    after restart) no earlier async LLM call captured that loop — the check-in
    call must bind it itself or the trace shows no judge cost."""
    import asyncio

    import app.ai.llm as llm_pkg
    import app.ai.llm.llm as llm_module
    from app.ai.agents.checkins._llm import call_small_model

    seen = {}

    class _FakeLLM:
        def __init__(self, model, **kw):
            pass

        def inference(self, prompt, *, system=None, usage_scope=None, usage_scope_ref_id=None):
            loop = llm_module._MAIN_LOOP
            seen["loop_running"] = bool(loop is not None and loop.is_running())
            seen["scope"] = usage_scope
            return "{}"

    monkeypatch.setattr(llm_module, "_MAIN_LOOP", None)
    monkeypatch.setattr(llm_pkg, "LLM", _FakeLLM)
    out = asyncio.run(call_small_model(
        object(), system="s", prompt="p", usage_scope="checkin_judge", usage_scope_ref_id="c-1",
    ))
    assert out == "{}"
    assert seen == {"loop_running": True, "scope": "checkin_judge"}
