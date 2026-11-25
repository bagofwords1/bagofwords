"""The LLM instance's PII redactor cache follows the agent scope, also under
concurrency (docs/design/pii-agent-policies.md).

A reused LLM instance can serve calls for different agent scopes at once. A
call must always get the redactor resolved for ITS scope — never the one a
previous scope left in the cache while a load for the new scope is pending.
"""

import asyncio
import types

import pytest

import app.ai.llm.llm as llm_mod
from app.ai.llm.llm import LLM
from app.ai.llm.usage_attribution import reset_usage_attribution, set_usage_attribution


def _bare_llm():
    """An LLM with just the PII state __init__ sets (no provider client)."""
    llm = object.__new__(LLM)
    llm._organization_id = "org-1"
    llm._usage_session_maker = object()
    llm.model = types.SimpleNamespace(provider_id="prov-1", provider=None)
    llm._pii_redactor_override = None
    llm._pii_redactor = None
    llm._pii_loaded = False
    llm._pii_entry = None
    return llm


async def _in_scope(llm, scope):
    token = set_usage_attribution({"data_source_ids": list(scope)})
    try:
        return await llm._aget_pii_redactor()
    finally:
        reset_usage_attribution(token)


def test_a_call_never_gets_the_previous_scopes_redactor_while_a_load_is_pending(monkeypatch):
    redactor_for = {("a",): "redactor-A", ("b",): "redactor-B"}
    gate = {}
    loads = []

    async def fake_load(org, sm, provider, scope):
        loads.append(scope)
        if scope in gate:
            await gate[scope].wait()
        return redactor_for[scope]

    monkeypatch.setattr(llm_mod, "load_redactor_for_call", fake_load)

    async def main():
        llm = _bare_llm()
        assert await _in_scope(llm, ["a"]) == "redactor-A"          # cache holds scope A
        gate[("b",)] = asyncio.Event()
        first = asyncio.create_task(_in_scope(llm, ["b"]))          # load for B paused
        await asyncio.sleep(0)
        second = asyncio.create_task(_in_scope(llm, ["b"]))         # B again, while B is loading
        await asyncio.sleep(0)
        assert not second.done() or second.result() != "redactor-A"
        gate[("b",)].set()
        got = await asyncio.gather(first, second)
        assert got == ["redactor-B", "redactor-B"]
        # and a call for A after that resolves A again, never B's
        assert await _in_scope(llm, ["a"]) == "redactor-A"
        return loads

    loads = asyncio.run(main())
    assert loads.count(("b",)) >= 1


def test_each_call_returns_its_own_result_when_loads_finish_out_of_order(monkeypatch):
    """Scope B's load finishes after scope C's: C's caller gets C, B's gets B,
    whatever the cache holds by then."""
    events = {("b",): asyncio.Event, ("c",): asyncio.Event}

    async def main():
        gates = {k: v() for k, v in events.items()}

        async def fake_load(org, sm, provider, scope):
            await gates[scope].wait()
            return f"redactor-{scope[0]}"

        monkeypatch.setattr(llm_mod, "load_redactor_for_call", fake_load)
        llm = _bare_llm()
        b = asyncio.create_task(_in_scope(llm, ["b"]))
        c = asyncio.create_task(_in_scope(llm, ["c"]))
        await asyncio.sleep(0)
        gates[("c",)].set()
        assert await c == "redactor-c"
        gates[("b",)].set()
        assert await b == "redactor-b"
        # the cache now holds B (stored last); a C call reloads rather than reuse B
        assert await asyncio.wait_for(_in_scope(llm, ["c"]), 1) == "redactor-c"

    asyncio.run(main())


def test_an_explicit_redactor_is_used_for_every_scope(monkeypatch):
    async def fail(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("loader called despite an explicit redactor")

    monkeypatch.setattr(llm_mod, "load_redactor_for_call", fail)
    llm = _bare_llm()
    llm._pii_redactor_override = "explicit"

    async def main():
        return [await _in_scope(llm, s) for s in (["a"], ["b"], [])]

    assert asyncio.run(main()) == ["explicit"] * 3
