"""Public input contracts for the internal artifact browser workflow."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.ai.tools.artifact_verification import build_artifact_verification_hint
from app.ai.tools.schemas.browser import BrowserNavigateInput
from app.services.artifact_preview_service import ArtifactPreviewService


@pytest.mark.parametrize(
    ("target", "expected_field", "expected_value"),
    [
        ({"url": "https://example.test/report"}, "url", "https://example.test/report"),
        ({"artifact_id": "artifact-123"}, "artifact_id", "artifact-123"),
    ],
)
def test_browser_navigate_accepts_exactly_one_target(target, expected_field, expected_value):
    """Navigation can address either an allowlisted URL or a saved artifact."""
    parsed = BrowserNavigateInput(**target, title="Checking the report")

    assert getattr(parsed, expected_field) == expected_value
    assert parsed.title == "Checking the report"


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"url": "https://example.test/report", "artifact_id": "artifact-123"},
    ],
)
def test_browser_navigate_rejects_missing_or_ambiguous_target(target):
    """The target is required and URL/artifact modes cannot be mixed."""
    with pytest.raises(ValidationError):
        BrowserNavigateInput(**target)


def test_static_page_without_interactions_does_not_recommend_browser_verification():
    hint = build_artifact_verification_hint(
        artifact_id="artifact-static",
        version=4,
        code="renderDashboard({ charts: [revenueChart, ordersChart] })",
    )

    assert hint["recommended"] is False
    assert hint["reason_codes"] == []
    assert hint["next_tool"] is None


def test_user_adjustable_backend_parameter_recommends_verification():
    hint = build_artifact_verification_hint(
        artifact_id="artifact-filtered",
        version=7,
        parameters=[
            {"name": "country", "type": "string", "source": "input", "query_ids": ["sales"]}
        ],
    )

    assert hint["recommended"] is True
    assert "backend_parameters" in hint["reason_codes"]
    assert hint["next_tool"] == "browser_navigate"
    assert hint["next_tool_input"] == {"artifact_id": "artifact-filtered"}
    assert hint["availability"] == "available"


def test_local_interaction_recommends_verification():
    hint = build_artifact_verification_hint(
        artifact_id="artifact-modal",
        version=2,
        code="const [selected, setSelected] = useState(null); return <button onClick={openDetails}>Details</button>",
    )

    assert hint["recommended"] is True
    assert "interactive_app" in hint["reason_codes"]
    assert hint["next_tool_input"] == {"artifact_id": "artifact-modal"}


def test_identity_parameters_alone_do_not_make_a_page_complex():
    hint = build_artifact_verification_hint(
        artifact_id="artifact-identity",
        version=1,
        parameters=[{"name": "organization_id", "type": "string", "source": "identity"}],
    )

    assert hint["recommended"] is False
    assert hint["reason_codes"] == []


def test_copy_or_css_only_edit_does_not_recommend_interaction_verification():
    old_code = "const total = useState(0); return <button onClick={refresh}>Refresh</button>; /* color: blue */"
    new_code = "const total = useState(0); return <button onClick={refresh}>Update</button>; /* color: green */"
    hint = build_artifact_verification_hint(
        artifact_id="artifact-cosmetic",
        version=8,
        code=new_code,
        previous_code=old_code,
    )

    assert hint["recommended"] is False
    assert hint["reason_codes"] == []


def test_multiple_datasets_without_interactions_do_not_imply_complexity():
    hint = build_artifact_verification_hint(
        artifact_id="artifact-multi-source",
        version=3,
        code="renderDashboard({ sources: [sales, inventory, customers] })",
    )

    assert hint["recommended"] is False


class _PreviewResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class _PreviewClient:
    """Replace only the external HTTP response boundary used by the service."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    async def post(self, *args, **kwargs):
        return _PreviewResponse(self.payload, self.status_code)


class _PreviewRoute:
    def __init__(self, *, params=None):
        self.request = SimpleNamespace(
            url="https://preview.test/api/queries/query-a/run",
            method="POST",
            post_data_json={"mode": "viewer", "params": params or {}},
        )
        self.response = None

    async def fulfill(self, *, status, json):
        self.response = {"status": status, "json": json}
        return self.response


def _preview_service():
    service = ArtifactPreviewService({
        "report": SimpleNamespace(id="report-1"),
        "organization": SimpleNamespace(id="org-1"),
        "user": SimpleNamespace(id="user-1"),
    })
    service.artifact = {"id": "artifact-1"}
    service.query_ids = {"query-a", "query-b"}
    service.origin = "https://preview.test"
    return service


async def _complete_query(service, action_id, *, applied_params, status="success", acknowledge=True):
    service.action_id = action_id
    service.client = _PreviewClient({"status": status, "applied_params": applied_params, "data": {"rows": []}})
    route = _PreviewRoute(params={"country": "CA"})
    await service.route(route)
    request_id = route.response["json"]["verification_request_id"]
    revision = f"revision-{action_id}"
    service.record("data_sent", action_id=action_id, data_revision=revision, request_ids=[request_id])
    if acknowledge:
        service.record("data_received", action_id=action_id, data_revision=revision)
    return route


@pytest.mark.asyncio
async def test_preview_wait_does_not_pass_when_server_applied_params_mismatch():
    service = _preview_service()
    action_id = service.begin_action()
    await _complete_query(service, action_id, applied_params={"country": "US"})

    status = await service.wait(
        action_id,
        {"query_ids": ["query-a"], "params": {"country": "CA"}},
        timeout=0.01,
    )

    assert status == "parameter_mismatch"


@pytest.mark.asyncio
async def test_preview_wait_passes_only_after_matching_query_and_runtime_acknowledgement():
    service = _preview_service()
    action_id = service.begin_action()
    route = await _complete_query(service, action_id, applied_params={"country": "CA"})

    status = await service.wait(
        action_id,
        {"query_ids": ["query-a"], "params": {"country": "CA"}},
        timeout=0.01,
    )
    evidence = service.evidence(since=0)

    assert status == "data_received"
    assert evidence["queries"][0]["execution_mode"] == "viewer_query"
    assert evidence["queries"][0]["request_id"] == route.response["json"]["verification_request_id"]
    assert {event["kind"] for event in evidence["runtime"]} == {"data_sent", "data_received"}


@pytest.mark.asyncio
async def test_preview_wait_reports_failed_server_query_even_when_http_succeeds():
    service = _preview_service()
    action_id = service.begin_action()
    await _complete_query(service, action_id, applied_params={"country": "CA"}, status="error")

    status = await service.wait(action_id, {"query_ids": ["query-a"]}, timeout=0.01)

    assert status == "failed"


@pytest.mark.asyncio
async def test_preview_wait_rejects_expectation_for_query_outside_artifact():
    service = _preview_service()
    action_id = service.begin_action()

    with pytest.raises(ValueError, match="belong to this artifact"):
        await service.wait(action_id, {"query_ids": ["query-from-another-artifact"]}, timeout=0.01)


@pytest.mark.asyncio
async def test_preview_wait_distinguishes_pending_work_from_no_expected_request():
    service = _preview_service()
    pending_action = service.begin_action()
    service.pending.add("request-still-running")

    pending = await service.wait(
        pending_action,
        {"query_ids": ["query-a"], "params": {"country": "CA"}},
        timeout=0.01,
    )

    service.pending.clear()
    no_request_action = service.begin_action()
    no_request = await service.wait(
        no_request_action,
        {"query_ids": ["query-a"], "params": {"country": "CA"}},
        timeout=0.01,
    )

    assert pending == "pending"
    assert no_request == "no_expected_request"


@pytest.mark.asyncio
async def test_preview_wait_does_not_attribute_a_previous_action_query_to_the_next_action():
    service = _preview_service()
    first_action = service.begin_action()
    await _complete_query(
        service,
        first_action,
        applied_params={"country": "CA"},
    )
    second_action = service.begin_action()
    second_status = await service.wait(second_action, {"query_ids": ["query-a"]}, timeout=0.01)
    evidence = service.evidence(since=service.actions[second_action])

    assert evidence["queries"] == []
    assert evidence["runtime"] == []
    assert second_status == "no_expected_request"


@pytest.mark.asyncio
async def test_stale_runtime_acknowledgement_does_not_complete_a_new_query():
    service = _preview_service()
    first_action = service.begin_action()
    first_route = await _complete_query(
        service,
        first_action,
        applied_params={"country": "CA"},
        acknowledge=False,
    )
    stale_revision = f"revision-{first_action}"
    first_status = await service.wait(
        first_action,
        {"query_ids": ["query-a"], "params": {"country": "CA"}},
        timeout=0.01,
    )

    second_action = service.begin_action()
    second_route = await _complete_query(
        service,
        second_action,
        applied_params={"country": "CA"},
        acknowledge=False,
    )
    # A late ACK may carry the prior render revision. It must not acknowledge
    # the second action's distinct query request ID.
    service.record("data_received", action_id=second_action, data_revision=stale_revision)

    status = await service.wait(
        second_action,
        {"query_ids": ["query-a"], "params": {"country": "CA"}},
        timeout=0.01,
    )
    evidence = service.evidence(since=0)
    query_events = evidence["queries"]

    assert first_route.response["json"]["verification_request_id"] != query_events[-1]["request_id"]
    assert query_events[0]["action_id"] == first_action
    assert query_events[-1]["action_id"] == second_action
    assert first_status == "data_not_acknowledged"
    assert second_route.response["json"]["verification_request_id"] == query_events[-1]["request_id"]
    assert status == "data_not_acknowledged"


@pytest.mark.parametrize('change', [
    ('function refresh() { setRows(oldRows) }', 'function refresh() { setRows(newRows) }'),
    ('const rows = vizById("summary-v1")', 'const rows = vizById("summary-v2")'),
])
def test_functional_edits_recommend_rechecking_even_with_unchanged_controls(change):
    controls = 'const [selected, setSelected] = useState(null); return <button onClick={refresh}>Refresh</button>'
    hint = build_artifact_verification_hint(artifact_id='artifact-new-version', version=2,
        previous_code=change[0] + controls, code=change[1] + controls)
    assert hint['recommended'] is True


@pytest.mark.parametrize(('returned', 'total', 'truncated'), [(2, 8, True), (5, 5, False), (0, 0, False), (2, None, False)])
@pytest.mark.asyncio
async def test_query_evidence_distinguishes_partial_results(returned, total, truncated):
    service = _preview_service()
    service.client = _PreviewClient({'status': 'success', 'data': {'rows': [{}] * returned, 'info': {'total_rows': total}}})
    await service.route(_PreviewRoute())
    query = service.evidence()['queries'][0]
    assert (query['returned_rows'], query['total_rows'], query['result_truncated']) == (returned, total, truncated)


@pytest.mark.parametrize(('path', 'body'), [
    ('/api/queries/query-a/run', {'mode': 'owner', 'params': {}}),
    ('/api/queries/query-a/run', {'mode': 'viewer', 'code': 'unsafe', 'params': {}}),
    ('/api/queries/foreign-query/run', {'mode': 'viewer', 'params': {}}),
    ('/api/reports/report-1', {'title': 'unauthorized change'}),
    ('/api/mcp/execute', {'tool': 'update_record'}),
])
@pytest.mark.asyncio
async def test_preview_rejects_unscoped_or_mutating_api_requests_before_dispatch(path, body):
    service = _preview_service()
    route = _PreviewRoute()
    route.request.url = service.origin + path
    route.request.post_data_json = body
    # No HTTP client exists: an accidental dispatch fails the test.
    await service.route(route)
    assert route.response['status'] == 403
    assert not service.pending


@pytest.mark.asyncio
async def test_query_requires_acknowledgement_of_every_expected_query():
    service = _preview_service()
    action_id = service.begin_action()
    await _complete_query(service, action_id, applied_params={'country': 'CA'})
    status = await service.wait(action_id, {'query_ids': ['query-a', 'query-b']}, timeout=0)
    assert status != 'data_received'


@pytest.mark.asyncio
async def test_runtime_exception_after_a_successful_query_is_not_a_pass():
    service = _preview_service()
    action_id = service.begin_action()
    await _complete_query(service, action_id, applied_params={'country': 'CA'})
    service.record('error', message='selectedRow is not defined', source='runtime')
    assert await service.wait(action_id, {'query_ids': ['query-a']}, timeout=0) == 'failed'
    assert service.evidence()['errors']


@pytest.mark.asyncio
async def test_access_denial_never_returns_previous_browser_evidence():
    from app.ai.tools.implementations._artifact_browser import artifact_operation_failure
    # A denied operation must not even access the old session/frame.
    result = await artifact_operation_failure(None, PermissionError('Data visibility restricted'), {})
    for payload in result.values():
        assert payload['success'] is False
        assert not {'snapshot', 'evidence', 'artifact', 'parameters'} & payload.keys()


def test_replay_keeps_internal_snapshot_refs_and_query_manifest_within_budget():
    import json
    from app.ai.persisted_summary import build_tool_context_summary, GENERIC_TOOL_CONTEXT_BUDGET_BYTES
    snapshot = '- generic: Revenue\n' * 200 + '- button "Close details" [ref=f1e501]\n'
    observation = {'success': True, 'artifact': {'id': 'saved-version', 'version': 4},
                   'parameters': [{'name': 'country', 'query_ids': ['ledger', 'summary']}],
                   'snapshot': snapshot, 'evidence': {'update_status': 'data_received'}}
    result = build_tool_context_summary('browser_act', observation, observation)
    assert 'f1e501' in result['observation']['snapshot']
    assert result['observation']['parameters'] == observation['parameters']
    assert len(json.dumps(result).encode()) <= GENERIC_TOOL_CONTEXT_BUDGET_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(('label', 'value'), [('Rock', '7'), ('Canada', 'CA')])
async def test_iframe_snapshot_refs_select_visible_labels_and_open_close_details(label, value):
    """Real browser contract: frame refs must support native selects and dialogs."""
    import asyncio
    import re
    from pathlib import Path
    import httpx
    from playwright.async_api import async_playwright
    from app.ai.tools.implementations._artifact_browser import run_artifact_operation
    from app.ai.tools.schemas.browser import BrowserActInput, BrowserSnapshotInput

    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip('Preprovisioned Chromium is required for the iframe contract')
        browser = await playwright.chromium.launch(headless=True)
        client = httpx.AsyncClient(base_url='https://preview.test', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'id': 'artifact-1'})))
        try:
            page = await browser.new_page()
            await page.set_content('<iframe></iframe>')
            frame = page.frames[1]
            await frame.set_content(f'''<main><h1>Revenue</h1><p>$23,456.78</p>
              <select aria-label="Category"><option value="">All</option><option value="{value}">{label}</option></select>
              <button onclick="document.querySelector('dialog').showModal()">Open details</button>
              <dialog aria-label="Transaction details"><p>Invoice details</p>
                <button onclick="document.querySelector('dialog').close()">Close details</button></dialog></main>''')
            service = _preview_service()
            service.artifact = {'id': 'artifact-1', 'version': 2}
            service.url = service.origin + '/artifact-preview/artifact-1'
            service.client = client
            session = SimpleNamespace(session_id='iframe-contract', frame=frame, page=page,
                                      preview=service, action_lock=asyncio.Lock())
            result = await run_artifact_operation(session, 'snapshot', BrowserSnapshotInput(session_id=session.session_id), {})
            assert '$23,456.78' in result['output']['snapshot']

            def ref(role, name):
                match = re.search(role + ' "' + re.escape(name) + r'"[^\n]*\[ref=([^\]]+)\]', result['output']['snapshot'])
                assert match, result['output']['snapshot']
                return match[1]

            result = await run_artifact_operation(session, 'act', BrowserActInput(
                session_id=session.session_id, ref=ref('combobox', 'Category'), action='select', text=label), {})
            assert await frame.locator('select').input_value() == value
            result = await run_artifact_operation(session, 'act', BrowserActInput(
                session_id=session.session_id, ref=ref('button', 'Open details'), action='click'), {})
            assert 'Invoice details' in result['output']['snapshot']
            result = await run_artifact_operation(session, 'act', BrowserActInput(
                session_id=session.session_id, ref=ref('button', 'Close details'), action='click'), {})
            assert not await frame.locator('dialog').is_visible()
            assert result['output']['evidence']['errors'] == []
        finally:
            await client.aclose()
            await browser.close()


@pytest.mark.asyncio
async def test_data_visibility_policy_denies_preview_before_authorized_transport_opens():
    service = ArtifactPreviewService({
        'report': SimpleNamespace(id='private-report'), 'organization': SimpleNamespace(id='private-org'),
        'user': SimpleNamespace(id='member'),
        'settings': SimpleNamespace(get_config=lambda key: SimpleNamespace(value=False)),
    })
    with pytest.raises(PermissionError):
        await service.open('private-artifact')
    assert service.client is None
    assert service.events == []


@pytest.mark.asyncio
async def test_internal_preview_cannot_be_reused_across_user_org_report_or_execution():
    from pathlib import Path
    from playwright.async_api import async_playwright
    from app.ai.tools.implementations._browser_common import BrowserSessionManager
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Preprovisioned Chromium is required for the session contract')
    manager = BrowserSessionManager()
    ctx = {'organization': SimpleNamespace(id='tenant-a'), 'user': SimpleNamespace(id='member-a'),
           'report': SimpleNamespace(id='report-a'), 'agent_execution_id': 'run-a'}
    try:
        session = await manager.open('report-a', ['https://example.test/*'], False, runtime_ctx=ctx, preview=ArtifactPreviewService(ctx))
        assert manager.get(session.session_id, ctx) is session
        assert manager.get(session.session_id) is None
        for key in ('organization', 'user', 'report', 'agent_execution_id'):
            other = {**ctx, key: 'run-b' if key == 'agent_execution_id' else SimpleNamespace(id='other')}
            assert manager.get(session.session_id, other) is None
        await manager.close_execution('run-a')
        assert manager.get(session.session_id, ctx) is None
    finally:
        await manager.close_execution('run-a')


@pytest.mark.asyncio
async def test_expected_filter_values_derive_all_affected_authorized_queries():
    service = _preview_service()
    service.queries = [
        {'id': 'query-a', 'parameters': [{'name': 'country'}, {'name': 'genre'}]},
        {'id': 'query-b', 'parameters': [{'name': 'country'}]},
    ]
    action_id = service.begin_action()
    await _complete_query(service, action_id, applied_params={'country': 'CA'})
    # Both queries declare country: one successful query cannot pass the filter.
    assert await service.wait(action_id, {'params': {'country': 'CA'}}, timeout=0) == 'no_expected_request'
    for params in ({}, {'unknown': 'value'}):
        with pytest.raises(ValueError):
            await service.wait(action_id, {'params': params}, timeout=0)
    # A more specific parameter affects only its declaring query.
    await _complete_query(service, action_id, applied_params={'country': 'CA', 'genre': 2})
    # Start a clean action so prior results cannot satisfy the new expectation.
    action_id = service.begin_action()
    await _complete_query(service, action_id, applied_params={'country': 'CA', 'genre': 2})
    assert await service.wait(action_id, {'params': {'genre': 2}}, timeout=0) == 'data_received'


@pytest.mark.asyncio
async def test_connector_browser_session_survives_a_turn_for_the_same_authorized_user():
    from pathlib import Path
    from playwright.async_api import async_playwright
    from app.ai.tools.implementations._browser_common import BrowserSessionManager
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Preprovisioned Chromium is required for the session contract')
    manager = BrowserSessionManager()
    ctx = {'organization': SimpleNamespace(id='tenant-connector'), 'user': SimpleNamespace(id='member-a'),
           'report': SimpleNamespace(id='report-connector'), 'agent_execution_id': 'first-turn'}
    try:
        session = await manager.open('report-connector', ['https://example.test/*'], False, runtime_ctx=ctx)
        await manager.close_execution('first-turn')
        following = {**ctx, 'agent_execution_id': 'following-turn'}
        assert manager.get(session.session_id, following) is session
        assert await manager.open('report-connector', ['https://example.test/*'], False, runtime_ctx=following) is session
        assert manager.get(session.session_id, {**following, 'user': SimpleNamespace(id='another-member')}) is None
    finally:
        await manager.close_report('report-connector')


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "expected", "applied"), [
    ("id", 7, "7"),
    ("date_range", {"from": "2024-03-01", "to": None}, {"from": "2024-03-01"}),
    ("date_range", {"from": "2024-03-01T08:00:00Z"}, {"from": "2024-03-01T10:00:00+02:00"}),
])
async def test_preview_compares_parameter_values_using_the_declared_type(kind, expected, applied):
    service = _preview_service()
    service.queries = [{"id": "query-a", "parameters": [{"name": "period", "type": kind}]}]
    action = service.begin_action()
    await _complete_query(service, action, applied_params={"period": applied})
    assert await service.wait(action, {"params": {"period": expected}}, timeout=0.01) == "data_received"


@pytest.mark.asyncio
async def test_empty_date_filtered_result_is_inconclusive_even_when_update_was_acknowledged():
    service = _preview_service()
    service.queries = [{"id": "query-a", "parameters": [{"name": "period", "type": "date_range"}]}]
    action = service.begin_action()
    bounds = {"from": "2024-03-01", "to": "2024-03-31"}
    await _complete_query(service, action, applied_params={"period": bounds})
    status = await service.wait(action, {"params": {"period": bounds}}, timeout=0.01)
    evidence = service.evidence(since=0, update_status=status)
    assert status == "data_received"  # Transport succeeded; business correctness is a separate check.
    assert any(c["code"] == "empty_date_result" and c["status"] == "inconclusive" for c in evidence["result_checks"])
