"""Sandbox helpers: `lists` and `browser` in generated code, and the `http`
URL budget (org "Sandbox" settings).

Contract:
- every Sandbox setting is off unless the org turns it on;
- generated code reaches `lists` / `browser` only when the executor was given
  them, in the subprocess sandbox and in-process alike, and only through the
  trusted-side handlers;
- rows the code submits are validated on submit and held; a failed attempt's
  rows are discarded before the retry runs;
- `http` stops at the per-run URL budget.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.sandbox_helpers import (
    BrowserUnavailableError,
    MeteredHttp,
    PageTextStore,
    SandboxHelpers,
    SandboxSettings,
    rerun_sandbox_helpers,
    sandbox_settings,
)
from app.services.agent_lists.sandbox_sink import SandboxList, SandboxListError, SandboxListSink


def _org(**values):
    return SimpleNamespace(get_config=lambda k: SimpleNamespace(value=values[k]) if k in values else None)


def _list(slug="companies", name="Companies", require_evidence=False):
    return SandboxList(
        id=f"L-{slug}", name=name, slug=slug, data_source_id="D1",
        fields=[
            {"id": "f1", "name": "company", "type": "string", "required": True},
            {"id": "f2", "name": "employees", "type": "integer"},
            {"id": "f3", "name": "tier", "type": "enum", "enum": ["gold", "silver"]},
        ],
        key_field_id="f1", require_evidence=require_evidence,
    )


def _sink(max_rows=100, lists=None):
    return SandboxListSink(lists=lists or [_list()], max_rows=max_rows)


# ── settings ────────────────────────────────────────────────────────────────

def test_every_sandbox_capability_is_off_by_default():
    from app.schemas.organization_settings_schema import OrganizationSettingsConfig

    cfg = OrganizationSettingsConfig()
    org = SimpleNamespace(get_config=lambda k: getattr(cfg, k, None))
    sb = sandbox_settings(org)
    assert not (sb.web_fetch or sb.browser or sb.lists or sb.allow_mutating_requests)
    # Missing settings (unit callers, legacy rows) are off too.
    assert sandbox_settings(None) == SandboxSettings()


@pytest.mark.parametrize("raw, expected", [(0, 1), (-5, 1), (50, 50), (10**9, 10_000), ("x", 1000)])
def test_url_budget_is_clamped(raw, expected):
    assert sandbox_settings(_org(sandbox_web_fetch_max_urls=raw)).max_urls == expected


# ── http budget ─────────────────────────────────────────────────────────────

class _FakeHttp:
    def __init__(self):
        self.fetched = []

    def get(self, url, **kw):
        self.fetched.append(url)
        return SimpleNamespace(url=url, final_url=url, success=True, error=None, text=f"body of {url}")

    def batch_get(self, urls, **kw):
        return [self.get(u) for u in urls]


def test_http_stops_at_the_per_run_url_budget():
    inner = _FakeHttp()
    http = MeteredHttp(inner, max_urls=5)
    http.batch_get([f"https://e.com/{i}" for i in range(4)])
    http.get("https://e.com/4")
    with pytest.raises(ValueError, match="at most 5 URLs"):
        http.get("https://e.com/5")
    # A batch that would cross the budget fetches nothing.
    with pytest.raises(ValueError):
        MeteredHttp(_FakeHttp(), max_urls=3).batch_get(["a", "b", "c", "d"])
    assert len(inner.fetched) == 5 and http.used == 5


def test_fetched_page_text_becomes_an_evidence_source():
    texts = PageTextStore()
    MeteredHttp(_FakeHttp(), max_urls=10, page_texts=texts).get("https://e.com/acme")
    from app.services.agent_lists.verify import verify_quote

    sources = texts.to_sources()
    assert verify_quote("body of https://e.com/acme", "https://e.com/acme", sources)
    assert not verify_quote("a sentence the page never had", None, sources)


# ── list sink ───────────────────────────────────────────────────────────────

def test_sink_accepts_flat_rows_by_slug_or_name_and_returns_counts_only():
    sink = _sink()
    out = sink.submit("companies", [{"company": "Acme", "employees": 10}])
    assert out == {"list": "companies", "queued": 1, "total_queued": 1}
    sink.submit("Companies", [{"company": "Globex", "tier": "gold"}])
    pending = sink.take()
    rows = pending["L-companies"]
    assert [r["fields"]["company"]["value"] for r in rows] == ["Acme", "Globex"]
    assert rows[0]["fields"]["company"]["status"] == "found"
    assert sink.queued() == 0


def test_invalid_values_raise_at_submit_and_nothing_is_queued():
    sink = _sink()
    good = [{"company": f"C{i}"} for i in range(250)]
    bad = good + [{"company": "Bad", "tier": "platinum"}]
    with pytest.raises(SandboxListError) as e:
        sink.submit("companies", bad)
    # The index points at the caller's row, past the 200-record chunk boundary.
    assert "records.250.fields.tier" in str(e.value)
    assert sink.queued() == 0


@pytest.mark.parametrize("ref", ["nope", "", None])
def test_unknown_list_names_the_lists_that_exist(ref):
    with pytest.raises(SandboxListError, match="companies"):
        _sink().submit(ref, [{"company": "Acme"}])


def test_row_limit_is_per_run_and_reset_clears_a_failed_attempt():
    sink = _sink(max_rows=3)
    sink.submit("companies", [{"company": "A"}, {"company": "B"}])
    with pytest.raises(SandboxListError, match="at most 3 rows"):
        sink.submit("companies", [{"company": "C"}, {"company": "D"}])
    assert sink.queued() == 2
    sink.reset()
    assert sink.queued() == 0
    sink.submit("companies", [{"company": "C"}, {"company": "D"}, {"company": "E"}])
    assert sink.queued() == 3


def test_evidence_rule_is_enforced_at_submit():
    sink = _sink(lists=[_list(require_evidence=True)])
    # `company` has method=extract by default only when declared; a found value
    # without a quote is rejected only for extract fields, so declare one.
    lst = sink.lists[0]
    fields = [dict(f, method="extract") if f["name"] == "company" else f for f in lst.fields]
    sink = _sink(lists=[SandboxList(**{**lst.__dict__, "fields": fields})])
    with pytest.raises(SandboxListError, match="exact quote"):
        sink.submit("companies", [{"company": "Acme"}])
    sink.submit("companies", [{"company": {"value": "Acme", "status": "found",
                                           "evidence": [{"quote": "Acme Corp", "ref": "https://e.com"}]}}])
    assert sink.queued() == 1


# ── wiring through the executor ─────────────────────────────────────────────

class _FakeBrowser:
    """Stands in for SandboxBrowser on the trusted side: records every op."""

    def __init__(self):
        self.ops = []
        self.closed = 0
        self.pages_loaded = 0
        self.blocked_requests = 0

    def call(self, op, args, kwargs):
        self.ops.append((op, list(args), dict(kwargs)))
        if op == "goto":
            self.pages_loaded += 1
            return {"url": args[0], "status": 200}
        if op == "query_all":
            return [{"text": "Acme", "href": "https://e.com/acme"}, {"text": "Globex", "href": "https://e.com/globex"}]
        return {"url": "https://e.com"}

    def close(self):
        self.closed += 1


CODE = '''
def generate_df(ds_clients, excel_files, lists, browser):
    browser.goto("https://e.com/companies")
    items = browser.query_all("a.company", attrs=["href"])
    out = lists.submit("companies", [{"company": i["text"]} for i in items])
    print("queued", out["queued"])
    return pd.DataFrame(items)
'''


@pytest.fixture(params=["subprocess", "inprocess"])
def sandbox_mode(request, monkeypatch):
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    return request.param


def test_code_drives_the_browser_and_queues_rows_through_the_helpers(sandbox_mode):
    sink, browser = _sink(), _FakeBrowser()
    helpers = SandboxHelpers(lists=sink, browser=browser)
    df, log, _ = StreamingCodeExecutor(organization_settings=None, sandbox_helpers=helpers).execute_code(
        code=CODE, ds_clients={}, excel_files=[],
    )
    assert list(df["text"]) == ["Acme", "Globex"]
    assert "queued 2" in log
    assert [op for op, *_ in browser.ops] == ["goto", "query_all"]
    assert sink.queued() == 2
    # The page is closed after every attempt.
    assert browser.closed == 1


def test_a_failed_attempt_leaves_nothing_queued_for_the_next(sandbox_mode):
    sink = _sink()
    executor = StreamingCodeExecutor(organization_settings=None, sandbox_helpers=SandboxHelpers(lists=sink))
    failing = '''
def generate_df(ds_clients, excel_files, lists):
    lists.submit("companies", [{"company": "Half"}])
    raise RuntimeError("page 2 failed")
'''
    with pytest.raises(Exception, match="page 2 failed"):
        executor.execute_code(code=failing, ds_clients={}, excel_files=[])
    assert sink.queued() == 1  # held, not saved
    ok = '''
def generate_df(ds_clients, excel_files, lists):
    lists.submit("companies", [{"company": "Whole"}])
    return pd.DataFrame([{"company": "Whole"}])
'''
    executor.execute_code(code=ok, ds_clients={}, excel_files=[])
    rows = sink.take()["L-companies"]
    assert [r["fields"]["company"]["value"] for r in rows] == ["Whole"]


def test_invalid_rows_surface_inside_the_code_where_it_can_react(sandbox_mode):
    sink = _sink()
    code = '''
def generate_df(ds_clients, excel_files, lists):
    try:
        lists.submit("companies", [{"company": "Acme", "tier": "platinum"}])
    except Exception as e:
        return pd.DataFrame([{"error": str(e)}])
    return pd.DataFrame()
'''
    df, _, _ = StreamingCodeExecutor(organization_settings=None, sandbox_helpers=SandboxHelpers(lists=sink)).execute_code(
        code=code, ds_clients={}, excel_files=[],
    )
    assert "records.0.fields.tier" in df["error"][0]
    assert sink.queued() == 0


def test_without_helpers_the_code_gets_none(sandbox_mode):
    """Reruns and dashboard refreshes build the executor without helpers."""
    code = '''
def generate_df(ds_clients, excel_files, lists, browser):
    return pd.DataFrame([{"lists": lists is None, "browser": browser is None}])
'''
    df, _, _ = StreamingCodeExecutor(organization_settings=None).execute_code(code=code, ds_clients={}, excel_files=[])
    assert bool(df["lists"][0]) and bool(df["browser"][0])


# ── coder prompt ────────────────────────────────────────────────────────────

def test_prompt_describes_only_the_helpers_this_run_has():
    from app.ai.tools.implementations.create_data import sandbox_helpers_directive

    sb = SandboxSettings(lists=True, browser=True)
    only_lists = sandbox_helpers_directive(_sink(), None, sb)
    assert "lists.submit" in only_lists and "'companies'" in only_lists and "browser.goto" not in only_lists
    only_browser = sandbox_helpers_directive(None, _FakeBrowser(), sb)
    assert "browser.goto" in only_browser and "lists.submit" not in only_browser
    assert "blocked" in only_browser
    assert "blocked" not in sandbox_helpers_directive(None, _FakeBrowser(), SandboxSettings(browser=True, allow_mutating_requests=True))
    assert sandbox_helpers_directive(None, None, sb) == ""


# ── code that drives `browser` outside the agent's own run ──────────────────
# Publish, a dashboard refresh and a reader's run re-execute saved code. Code
# that drives the browser must either get one or stop with a reason; it used
# to crash on `None.goto`.

def _browser_source(patterns):
    conn = SimpleNamespace(type="browser", config={"url_patterns": patterns, "allow_downloads": False})
    return SimpleNamespace(connections=[SimpleNamespace(type="sqlite", config={}), conn])


@pytest.mark.parametrize("call", ['browser.goto("https://e.com")', 'browser.text()', 'x = browser.query_all("a")'])
def test_code_using_the_browser_without_one_stops_with_the_reason(sandbox_mode, call):
    code = f"def generate_df(ds_clients, excel_files, browser=None):\n    {call}\n    return pd.DataFrame()\n"
    for settings, reason in ((None, "setting is off"),
                             (_org(enable_sandbox_browser=True), "no browser connection")):
        with pytest.raises(BrowserUnavailableError, match=reason) as e:
            StreamingCodeExecutor(organization_settings=settings).execute_code(code=code, ds_clients={}, excel_files=[])
        assert "NoneType" not in str(e.value)


@pytest.mark.parametrize("code", [
    # The code's own variable named browser is not the helper.
    'def generate_df(ds_clients, excel_files):\n    browser = "Firefox"\n'
    '    return pd.DataFrame([{"b": browser.upper()}])\n',
    'def label(browser):\n    return browser.title()\n\n'
    'def generate_df(ds_clients, excel_files):\n    return pd.DataFrame([{"b": label("edge")}])\n',
    # Only mentions the word.
    'def generate_df(ds_clients, excel_files):\n    return pd.DataFrame([{"b": "browser.goto"}])\n',
])
def test_code_that_does_not_drive_the_helper_runs_without_one(sandbox_mode, code):
    df, _, _ = StreamingCodeExecutor(organization_settings=None).execute_code(code=code, ds_clients={}, excel_files=[])
    assert len(df) == 1


def test_a_rerun_gets_a_browser_only_when_the_code_uses_it_and_the_org_allows_it():
    import asyncio

    uses = 'def generate_df(ds_clients, excel_files, browser):\n    browser.goto("https://e.com/x")\n'
    plain = 'def generate_df(ds_clients, excel_files):\n    return pd.DataFrame()\n'
    on = _org(enable_sandbox_browser=True)
    sources = [SimpleNamespace(connections=[]), _browser_source(["https://e.com/**"])]

    async def build(code, sources, settings):
        return rerun_sandbox_helpers(code, sources, settings)

    helpers = asyncio.run(build(uses, sources, on))
    assert helpers is not None and helpers.browser is not None and helpers.lists is None
    assert helpers.browser._patterns == ["https://e.com/**"]
    assert asyncio.run(build(plain, sources, on)) is None
    assert asyncio.run(build(uses, sources, _org())) is None
    assert asyncio.run(build(uses, [SimpleNamespace(connections=[])], on)) is None
