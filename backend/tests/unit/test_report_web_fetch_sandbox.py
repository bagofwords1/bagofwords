"""web_fetch switched off on a conversation (report.tools) also removes the
sandbox `http` client that generated code would otherwise get."""
from types import SimpleNamespace

import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor


def _org(web_fetch_on: bool):
    return SimpleNamespace(get_config=lambda key: SimpleNamespace(value=web_fetch_on) if key == "enable_web_fetch" else None)


@pytest.mark.parametrize("tools, expect_client", [
    (None, True),
    ({"clarify": False}, True),
    ({"web_fetch": False}, False),
], ids=["no-settings", "other-switch", "web_fetch-off"])
def test_sandbox_http_client_follows_the_report(tools, expect_client):
    hub = SimpleNamespace(report=SimpleNamespace(tools=tools))
    executor = StreamingCodeExecutor(organization_settings=_org(True), context_hub=hub)
    assert (executor._build_http_client() is not None) is expect_client


def test_org_setting_off_still_wins():
    hub = SimpleNamespace(report=SimpleNamespace(tools=None))
    executor = StreamingCodeExecutor(organization_settings=_org(False), context_hub=hub)
    assert executor._build_http_client() is None
