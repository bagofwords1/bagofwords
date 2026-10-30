"""Sub-agents can browse (docs/design/sub-agents.md A6).

- Every profile offers the browser tools (they are still gated per run by the
  report's browser connection, like for the parent).
- Each child gets its own connector browser session: parallel children and the
  parent never share a page, while one child keeps its page across its turns.
"""
import uuid
from types import SimpleNamespace

import pytest

from app.ai.sub_agent_profiles import PROFILES, allowed_tools
from app.ai.tools.implementations._browser_common import BrowserSessionManager

BROWSER_TOOLS = {"browser_navigate", "browser_snapshot", "browser_act", "browser_extract", "browser_vision"}


@pytest.mark.parametrize("profile", list(PROFILES.values()), ids=list(PROFILES))
@pytest.mark.parametrize("allow_create_data", [False, True])
def test_every_profile_offers_browser_tools(profile, allow_create_data):
    assert BROWSER_TOOLS <= allowed_tools(profile, allow_create_data=allow_create_data)


def _obj():
    return SimpleNamespace(id=uuid.uuid4())


def _ctx(org, user, report, *, sub_agent_id=None, execution_id=None):
    return {"organization": org, "user": user, "report": report,
            "sub_agent_id": sub_agent_id, "agent_execution_id": execution_id or str(uuid.uuid4())}


def test_connector_browser_scope_is_per_sub_agent():
    org, user, report = _obj(), _obj(), _obj()
    child_a, child_b = str(uuid.uuid4()), str(uuid.uuid4())
    scope = lambda **kw: BrowserSessionManager.scope_for(_ctx(org, user, report, **kw), internal=False)

    parent = scope()
    a = scope(sub_agent_id=child_a)
    b = scope(sub_agent_id=child_b)
    assert len({parent, a, b}) == 3
    # The parent keeps one session across its turns; so does each child.
    assert scope() == parent
    assert scope(sub_agent_id=child_a) == a


def test_internal_preview_scope_stays_per_execution():
    org, user, report = _obj(), _obj(), _obj()
    execution = str(uuid.uuid4())
    as_child = BrowserSessionManager.scope_for(_ctx(org, user, report, sub_agent_id=str(uuid.uuid4()), execution_id=execution))
    as_parent = BrowserSessionManager.scope_for(_ctx(org, user, report, execution_id=execution))
    assert as_child == as_parent
