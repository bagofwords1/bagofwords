"""The roster gate (``decide_focus_mode``) is the single source of truth for how
much a report pre-loads, shared by the schema roster and the standing
<instructions> scope. These pin the three regimes:

  - few agents, no explicit focus  → "all"   (render/scope everything)
  - explicit report focus           → "focus" (honor the pick)
  - many agents, nothing picked      → "pick"  (roster only; globals-only for
                                                instructions until an agent is picked)
"""
from app.ai.context.agent_roster import decide_focus_mode, DEFAULT_INDEX_THRESHOLD


def _roster(n):
    return {f"a{i}" for i in range(n)}


def test_few_agents_no_focus_is_all():
    ids = _roster(3)  # <= DEFAULT_INDEX_THRESHOLD (4)
    focus, mode = decide_focus_mode(ids, [], len(ids))
    assert mode == "all"
    assert focus == []


def test_at_threshold_is_still_all():
    ids = _roster(DEFAULT_INDEX_THRESHOLD)
    focus, mode = decide_focus_mode(ids, [], len(ids))
    assert mode == "all"


def test_many_agents_nothing_picked_is_pick():
    ids = _roster(DEFAULT_INDEX_THRESHOLD + 1)
    focus, mode = decide_focus_mode(ids, [], len(ids))
    assert mode == "pick"
    assert focus == []  # nothing pre-loaded; instruction scope becomes globals-only


def test_explicit_focus_wins_even_below_threshold():
    ids = _roster(2)
    focus, mode = decide_focus_mode(ids, ["a1"], len(ids))
    assert mode == "focus"
    assert focus == ["a1"]


def test_explicit_focus_is_filtered_to_roster():
    ids = _roster(10)
    # "ghost" is not attached; it must be dropped so scope never references a
    # non-attached agent.
    focus, mode = decide_focus_mode(ids, ["a1", "ghost"], len(ids))
    assert mode == "focus"
    assert focus == ["a1"]


def test_explicit_all_ghosts_falls_back_to_pick():
    ids = _roster(10)
    focus, mode = decide_focus_mode(ids, ["ghost1", "ghost2"], len(ids))
    # Every explicit id was filtered out → treated as "nothing picked".
    assert mode == "pick"
    assert focus == []
