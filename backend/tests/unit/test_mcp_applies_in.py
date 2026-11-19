"""applies_in: where a rule takes effect, in words an MCP client repeats.

It is built from the caller's rendered placement, so it never counts targets
the caller can't see, and it says first when the rule reaches no report yet.
"""
import pytest

from app.ai.tools.mcp.instructions import applies_in


def _placement(folders=0, agents=0, hidden=0, scope="targeted"):
    return {"scope": scope, "folders": [{"id": f"f{i}"} for i in range(folders)],
            "agents": [{"id": f"a{i}"} for i in range(agents)], "hidden_target_count": hidden}


@pytest.mark.parametrize("visible,hidden", [(1, 2), (2, 5), (1, 7)])
def test_hidden_targets_are_never_counted(visible, hidden):
    text = applies_in(_placement(folders=visible, hidden=hidden))
    assert f"{visible} folder" in text
    assert str(visible + hidden) not in text and f"{hidden} " not in text
    # ...but the caller still learns the rule reaches further than they see.
    assert text != applies_in(_placement(folders=visible))


@pytest.mark.parametrize("placement", [
    _placement(folders=1), _placement(agents=2), _placement(folders=1, agents=1), _placement(scope="global"),
])
@pytest.mark.parametrize("pending", [True, False])
def test_a_rule_not_in_effect_says_so_before_where_it_will_apply(placement, pending):
    live = applies_in(placement)
    idle = applies_in(placement, in_effect=False, pending=pending)
    assert idle != live and idle.lower().startswith("no report")
    assert applies_in(placement, in_effect=False, pending=not pending) != idle


def test_a_rule_with_no_target_applies_nowhere():
    assert applies_in(_placement()).lower().startswith("no report")
    assert applies_in(None).lower().startswith("no report")
