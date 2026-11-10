"""One context budget for the planner prompt — contract tests.

  - every threshold derives from the RECEIVING model's window, in order
  - the token anchor calibrates the estimate with the provider's count
  - the last-resort trim never cuts the rolling summary or the opening
    exchange, and drops whole oldest conversation lines first
  - history decay keeps ids while shrinking older messages
  - the governor runs the ladder in order: nothing below the cheap threshold,
    decay before folding, at most one synchronous fold per run
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.context.context_budget import (
    TokenAnchor,
    budget_for,
    estimate_planner_input_tokens,
)
from app.ai.context.context_hub import trim_context_to_budget
from app.ai.context.sections.messages_section import MessageItem, MessagesSection


def _model(window):
    m = MagicMock()
    m.context_window_tokens = window
    return m


# ------------------------------------------------------------------ budget


@pytest.mark.parametrize("window", [8_000, 16_000, 32_000, 128_000, 200_000, 1_000_000])
def test_budget_thresholds_are_ordered_and_window_derived(window):
    b = budget_for(_model(window))
    assert b.window == window
    assert 0 < b.target < b.cheap_at < b.summarize_at < b.usable < b.window
    assert b.output_reserve == window - b.usable
    # The protected tail and the summary always leave room for the rest.
    assert b.tail_tokens <= b.usable * 0.2 + 1
    assert b.summary_max <= max(b.usable * 0.1, 500) + 1


def test_budget_grows_with_window_and_shrinks_with_overflow_factor():
    small, large = budget_for(_model(32_000)), budget_for(_model(200_000))
    assert large.summarize_at > small.summarize_at
    assert large.tail_tokens >= small.tail_tokens
    tightened = budget_for(_model(200_000), factor=0.5)
    assert tightened.summarize_at < large.summarize_at
    assert tightened.window == large.window


def test_budget_falls_back_when_window_unknown():
    assert budget_for(_model(None)).window == budget_for(_model(0)).window > 0


def test_threshold_ratios_overridable_for_testing(monkeypatch):
    monkeypatch.setenv("BOW_CONTEXT_SUMMARIZE_RATIO", "0.3")
    monkeypatch.setenv("BOW_CONTEXT_CHEAP_RATIO", "0.2")
    monkeypatch.setenv("BOW_CONTEXT_TARGET_RATIO", "0.1")
    b = budget_for(_model(100_000))
    assert b.summarize_at == int(b.usable * 0.3)


# ------------------------------------------------------------------ anchor


@pytest.mark.parametrize("actual,estimate,later", [(30_000, 21_000, 25_000), (9_000, 12_000, 14_000)])
def test_anchor_calibrates_estimate_with_provider_count(actual, estimate, later):
    a = TokenAnchor()
    assert a.measure(later) == later  # no evidence yet → the estimate
    a.record(actual, estimate)
    assert a.measure(estimate) == actual
    assert a.measure(later) == later + (actual - estimate)


def test_anchor_never_erases_more_than_half_the_estimate():
    a = TokenAnchor()
    a.record(1_000, 50_000)
    assert a.measure(10_000) == 5_000


def test_estimate_counts_transcript_or_observations_not_both():
    transcript = MagicMock()
    transcript.turns = [1]
    transcript.tokens.return_value = 5_000
    pi = SimpleNamespace(messages_context="x" * 3_000, past_observations=[{"a": "b" * 300}], transcript=transcript)
    assert estimate_planner_input_tokens(pi) == 1_000 + 5_000


# ------------------------------------------------------------------ trim


def _section(n_lines: int, *, summary: str, opening: str) -> MessagesSection:
    head = [MessageItem(role="user", timestamp="09:00", text=opening)]
    body = []
    for i in range(n_lines):
        body.append(MessageItem(
            role="user" if i % 2 == 0 else "system",
            timestamp="10:00",
            text=f"line-{i} " + ("detail " * 60),
        ))
    return MessagesSection(items=head + body, history_summary=summary, head_count=1)


@pytest.mark.parametrize("n_lines,window", [(60, 12_000), (200, 12_000), (400, 14_000)])
def test_trim_never_cuts_summary_or_opening_exchange(n_lines, window):
    summary = "SUMMARY-SENTINEL " + ("fact " * 400)
    opening = "OPENING-SENTINEL what are my sales"
    rendered = _section(n_lines, summary=summary, opening=opening).render()
    pi = SimpleNamespace(messages_context=rendered, past_observations=None, last_observation=None)
    trim_context_to_budget(pi, model_context_window=window)

    out = pi.messages_context
    assert len(out) < len(rendered)
    assert "SUMMARY-SENTINEL" in out and out.count("fact ") == summary.count("fact ")
    assert "OPENING-SENTINEL" in out
    assert f"line-{n_lines - 1} " in out          # newest kept
    assert "line-0 " not in out                    # oldest dropped first
    # Whole lines only: every surviving conversation line is complete.
    body = out.split("<conversation>")[1].split("</conversation>")[0]
    for line in body.strip().split("\n")[1:]:
        assert line.startswith(("User", "Assistant")), line[:40]


def test_render_decay_shrinks_older_history_but_keeps_ids():
    items = []
    for i in range(12):
        items.append(MessageItem(
            role="system", timestamp="10:00",
            text=f"Tool: create_data → query_id: q-{i:04d}-abcdef " + ("rows " * 80),
        ))
    sec = MessagesSection(items=items)
    full, lvl1, lvl2 = sec.render(), sec.render(decay_level=1), sec.render(decay_level=2)
    assert len(full) > len(lvl1) > len(lvl2)
    for i in range(12):
        assert f"q-{i:04d}-abcdef" in lvl2


# ------------------------------------------------------------------ governor


def _agent(window: int, *, anchor_offset: int = 0):
    import app.ai.agent_v2 as agent_v2_mod

    agent = object.__new__(agent_v2_mod.AgentV2)
    agent.model = _model(window)
    agent.small_model = _model(window)
    agent.report = MagicMock()
    agent._context_budget_factor = 1.0
    agent._ctx_anchor = TokenAnchor(offset=anchor_offset, known=True)
    agent._ctx_anchor_loaded = True
    agent._sync_fold_done = False
    agent._history_reload_pending = False
    agent._compaction_attempted = False
    agent._schedule_background_fold = MagicMock()
    agent.transcript = SimpleNamespace(turns=[], history_turn_count=0)
    agent.context_hub = MagicMock()
    agent.context_hub.get_view.return_value = SimpleNamespace(warm=SimpleNamespace(messages=None))
    return agent


def _planner_input(chars: int):
    return SimpleNamespace(messages_context="m" * chars, transcript=None, past_observations=None)


def test_governor_does_nothing_below_cheap_threshold():
    agent = _agent(100_000)
    agent._sync_fold = AsyncMock()
    b = budget_for(agent.model)
    pi = _planner_input((b.cheap_at - 1_000) * 3)
    before = pi.messages_context
    stats = asyncio.run(agent._govern_context(pi, None))
    assert pi.messages_context == before
    assert stats["rungs"] == []
    agent._schedule_background_fold.assert_not_called()
    agent._sync_fold.assert_not_called()


def test_governor_decays_history_before_folding():
    """Over the cheap threshold, a cheap rung that brings the prompt back
    under budget means no synchronous LLM fold — only a background one."""
    agent = _agent(100_000)
    agent._sync_fold = AsyncMock()
    b = budget_for(agent.model)
    items = [MessageItem(role="system", timestamp="10:00", text=f"Tool: x → query_id: q-{i} " + "z" * 7_000) for i in range(40)]
    sec = MessagesSection(items=items)
    pi = SimpleNamespace(messages_context=sec.render(), transcript=None, past_observations=None)
    # Size the anchor so the full render lands between cheap and summarize.
    est = estimate_planner_input_tokens(pi)
    agent._ctx_anchor = TokenAnchor(offset=int((b.cheap_at + b.summarize_at) / 2) - est, known=True)

    stats = asyncio.run(agent._govern_context(pi, sec))
    assert any(r.startswith("history_decay") for r in stats["rungs"])
    assert stats["after"] < stats["before"]
    agent._schedule_background_fold.assert_called_once()
    agent._sync_fold.assert_not_called()


def test_governor_folds_synchronously_once_per_run_when_over_summarize():
    agent = _agent(100_000)
    b = budget_for(agent.model)
    folded = MessagesSection(items=[MessageItem(role="user", text="recent ask")], history_summary="short summary")

    async def _fake_fold(mode="auto_sync"):
        agent.context_hub.get_view.return_value = SimpleNamespace(warm=SimpleNamespace(messages=folded))
        return {"status": "compacted"}

    agent._sync_fold = AsyncMock(side_effect=_fake_fold)
    pi = _planner_input((b.summarize_at + 5_000) * 3)
    stats = asyncio.run(agent._govern_context(pi, None))
    agent._sync_fold.assert_awaited_once()
    assert "short summary" in pi.messages_context
    assert stats["after"] < b.summarize_at

    # Still over budget on the next request of the same run: no second fold.
    pi2 = _planner_input((b.summarize_at + 5_000) * 3)
    asyncio.run(agent._govern_context(pi2, None))
    agent._sync_fold.assert_awaited_once()


def test_governor_measures_with_anchor():
    """A prompt whose estimate looks small but whose provider-reported size
    is large (big fixed prompt) still triggers the ladder."""
    agent = _agent(100_000)
    agent._sync_fold = AsyncMock(return_value={"status": "nothing_to_compact"})
    b = budget_for(agent.model)
    pi = _planner_input((b.tail_tokens + max(b.summary_max, int(b.usable * 0.1))) * 3 + 300)  # history worth folding
    est = estimate_planner_input_tokens(pi)
    assert est < b.cheap_at
    agent._ctx_anchor = TokenAnchor(offset=b.summarize_at + 1_000 - est, known=True)
    asyncio.run(agent._govern_context(pi, None))
    agent._schedule_background_fold.assert_called_once()
    agent._sync_fold.assert_awaited_once()


def test_governor_skips_a_fold_that_would_not_pay_off():
    """Over budget mostly because of the fixed prompt, with only a little
    history beyond the tail: no synchronous fold (it would add an LLM
    round-trip before every reply and save almost nothing)."""
    agent = _agent(100_000)
    agent._sync_fold = AsyncMock()
    b = budget_for(agent.model)
    pi = _planner_input((b.tail_tokens + 500) * 3)
    est = estimate_planner_input_tokens(pi)
    agent._ctx_anchor = TokenAnchor(offset=b.summarize_at + 2_000 - est, known=True)
    asyncio.run(agent._govern_context(pi, None))
    agent._sync_fold.assert_not_called()


def test_governor_uses_a_finished_background_fold_without_another_llm_call():
    agent = _agent(100_000)
    agent._sync_fold = AsyncMock()
    agent._sync_fold_done = True
    agent._history_reload_pending = True
    folded = MessagesSection(items=[MessageItem(role="user", text="recent")], history_summary="bg summary")

    async def _reload():
        agent._history_reload_pending = False
        return SimpleNamespace(warm=SimpleNamespace(messages=folded))

    agent._reload_history_after_fold = AsyncMock(side_effect=_reload)
    b = budget_for(agent.model)
    pi = _planner_input((b.summarize_at + 5_000) * 3)
    asyncio.run(agent._govern_context(pi, None))
    agent._reload_history_after_fold.assert_awaited_once()
    agent._sync_fold.assert_not_called()
    assert "bg summary" in pi.messages_context


def test_governor_never_folds_an_empty_history():
    """When the fixed prompt alone is over budget there is nothing to fold:
    no fold, no background task, no "Compacting…" event."""
    agent = _agent(100_000)
    agent._sync_fold = AsyncMock()
    b = budget_for(agent.model)
    agent._ctx_anchor = TokenAnchor(offset=b.usable, known=True)
    pi = _planner_input(300)
    asyncio.run(agent._govern_context(pi, None))
    agent._schedule_background_fold.assert_not_called()
    agent._sync_fold.assert_not_called()


@pytest.mark.parametrize("with_summary", [False, True])
def test_trim_cuts_single_line_history_without_touching_summary(with_summary):
    """History with no line boundaries (one giant line) is still trimmed —
    oldest characters first — and a pinned summary still survives."""
    summary_block = "<history_summary>\nSUMMARY-SENTINEL\n</history_summary>\n" if with_summary else ""
    body = "OLDEST" + ("m" * 200_000) + "NEWEST"
    text = f"{summary_block}<conversation>{body}</conversation>" if with_summary else body
    pi = SimpleNamespace(messages_context=text, past_observations=None, last_observation=None)
    trim_context_to_budget(pi, model_context_window=20_000)
    out = pi.messages_context
    assert len(out) < len(text)
    assert "NEWEST" in out and "OLDEST" not in out
    assert ("SUMMARY-SENTINEL" in out) == with_summary


def test_governor_keeps_sub_agents_isolated_from_report_history():
    """A child's prompt carries no report conversation. Budget pressure must
    never put the parent's history back, nor fold the parent's report."""
    agent = _agent(100_000)
    agent._is_sub_agent = True
    agent._sync_fold = AsyncMock()
    b = budget_for(agent.model)
    parent = MessagesSection(items=[MessageItem(role="user", text="PARENT_ONLY_REQUEST " + "x" * 9000)])
    agent._ctx_anchor = TokenAnchor(offset=b.usable, known=True)
    pi = SimpleNamespace(messages_context="", transcript=None, past_observations=None)
    asyncio.run(agent._govern_context(pi, parent))
    assert pi.messages_context == ""
    agent._schedule_background_fold.assert_not_called()
    agent._sync_fold.assert_not_called()


def test_trim_never_cuts_a_summary_only_history():
    summary = "<history_summary>\nSUMMARY-SENTINEL " + ("fact " * 3000) + "\n</history_summary>"
    pi = SimpleNamespace(messages_context=summary, past_observations=None, last_observation=None)
    trim_context_to_budget(pi, model_context_window=12_000)
    assert pi.messages_context == summary


def test_governor_folds_when_the_live_window_drops_rows_even_under_budget():
    """Short turns never reach the token thresholds; a full live window must
    still trigger a fold, or the oldest turns vanish unsummarized."""
    agent = _agent(200_000)
    folded = MessagesSection(items=[MessageItem(role="user", text="recent")], history_summary="window summary")

    async def _fake_fold(mode="auto_sync", max_tail_rows=None):
        assert max_tail_rows and max_tail_rows > 0
        agent.context_hub.get_view.return_value = SimpleNamespace(warm=SimpleNamespace(messages=folded))
        return {"status": "compacted"}

    agent._sync_fold = AsyncMock(side_effect=_fake_fold)
    section = MessagesSection(items=[MessageItem(role="user", text="hi")], dropped_count=3)
    pi = _planner_input(300)
    asyncio.run(agent._govern_context(pi, section))
    agent._sync_fold.assert_awaited_once()
    assert "window summary" in pi.messages_context

    # A child never folds the parent's window.
    child = _agent(200_000)
    child._is_sub_agent = True
    child._sync_fold = AsyncMock()
    asyncio.run(child._govern_context(_planner_input(300), section))
    child._sync_fold.assert_not_called()


@pytest.mark.parametrize("answer_chars", [6_000, 40_000])
def test_trim_keeps_the_end_of_an_oversized_newest_message(answer_chars):
    summary = "<history_summary>\nSUMMARY\n</history_summary>\n"
    body = "User (10:00): short question\nAssistant (10:01): " + ("x" * answer_chars) + " NEWEST_RESULT_SENTINEL\n"
    text = summary + "<conversation>\n" + body + "</conversation>"
    pi = SimpleNamespace(messages_context=text, past_observations=None, last_observation=None)
    trim_context_to_budget(pi, model_context_window=10_000)
    out = pi.messages_context
    assert len(out) < len(text)
    assert "SUMMARY" in out
    assert "NEWEST_RESULT_SENTINEL" in out


@pytest.mark.parametrize("cache_read,cache_write", [(150_000, 0), (0, 150_000), (100_000, 40_000)])
def test_anthropic_cached_input_counts_toward_the_measured_prompt(cache_read, cache_write):
    """Anthropic reports cache reads/writes outside prompt_tokens; the anchor
    must calibrate against the whole prompt the provider processed."""
    from app.ai.context.context_budget import total_input_tokens
    usage = SimpleNamespace(prompt_tokens=5_000, cache_read_tokens=cache_read, cache_creation_tokens=cache_write)
    total = 5_000 + cache_read + cache_write
    assert total_input_tokens(usage, "anthropic") == total
    # Other providers already include the cache in prompt_tokens.
    assert total_input_tokens(SimpleNamespace(prompt_tokens=total, cache_read_tokens=cache_read), "openai") == total

    import app.ai.agent_v2 as agent_v2_mod
    agent = _agent(200_000)
    agent._ctx_anchor = TokenAnchor()
    agent._last_request_estimate = 60_000
    agent._last_planner_prompt_tokens = None
    agent._run_efficiency = MagicMock()
    agent._is_sub_agent = False
    agent.model.provider = SimpleNamespace(provider_type="anthropic")
    agent.context_hub.metadata = SimpleNamespace(section_sizes={}, total_tokens=0)
    agent._publish_context_metadata_to_view = MagicMock()
    decision = SimpleNamespace(metrics=SimpleNamespace(token_usage=usage))
    agent._record_planner_token_metadata_from_decision(decision)
    assert agent._last_planner_prompt_tokens == total
    assert agent._ctx_anchor.measure(60_000) == total


def test_persisted_anchor_is_one_requests_actual_and_estimate():
    """A later request that never reports usage (stopped run) must not pair
    its estimate with the previous request's actual."""
    import app.ai.agent_v2 as agent_v2_mod
    from app.services.context_compaction_service import ContextCompactionService

    agent = _agent(200_000)
    agent._ctx_anchor = TokenAnchor()
    agent._ctx_anchor_pair = None
    agent._last_planner_prompt_tokens = None
    agent._run_efficiency = MagicMock()
    agent._is_sub_agent = False
    agent.report_id = "r1"
    agent.model.provider = SimpleNamespace(provider_type="openai")
    agent.context_hub.metadata = SimpleNamespace(section_sizes={}, total_tokens=0)
    agent._publish_context_metadata_to_view = MagicMock()

    agent._last_request_estimate = 20_000  # request A
    agent._record_planner_token_metadata_from_decision(
        SimpleNamespace(metrics=SimpleNamespace(token_usage=SimpleNamespace(prompt_tokens=64_000)))
    )
    agent._last_request_estimate = 45_000  # request B: stopped, no usage

    class _S:
        async def __aenter__(self):
            return "session"
        async def __aexit__(self, *a):
            return False
    agent._session_maker = lambda: _S()
    calls = []

    async def _record(session, report_id, tokens, estimate):
        calls.append((tokens, estimate))
    from unittest.mock import patch
    with patch.object(ContextCompactionService, "record_prompt_anchor", side_effect=_record):
        asyncio.run(agent._persist_ctx_anchor())
    assert calls == [(64_000, 20_000)]
