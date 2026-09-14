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
    service = ArtifactPreviewService(_preview_runtime_context())
    service.artifact = {"id": "artifact-1"}
    service.query_ids = {"query-a", "query-b"}
    service.origin = "https://preview.test"
    return service


def _preview_runtime_context():
    return {
        "report": SimpleNamespace(id="report-1"),
        "organization": SimpleNamespace(id="org-1"),
        "user": SimpleNamespace(id="user-1"),
    }


async def _complete_query(service, action_id, *, applied_params, status="success", acknowledge=True, data=None):
    service.action_id = action_id
    service.client = _PreviewClient({
        "status": status,
        "applied_params": applied_params,
        "data": data or {"rows": []},
    })
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bounds", "issue"),
    [
        ({"from": "2024-06-02", "to": "2024-06-01"}, "reversed_date_range"),
        ({"from": "2024-06-01", "to": "2024-06-02T12:00:00Z"}, "mixed_date_range"),
        ({"from": "2024-06-01T12:00:00", "to": "2024-06-01T13:00:00Z"}, "mixed_date_range"),
    ],
)
async def test_date_range_evidence_flags_ambiguous_successful_results_as_inconclusive(bounds, issue):
    service = _preview_service()
    service.queries = [{"id": "query-a", "parameters": [{"name": "period", "type": "date_range"}]}]
    action_id = service.begin_action()
    await _complete_query(service, action_id, applied_params={"period": bounds}, data={"rows": [{"id": 1}]})

    status = await service.wait(action_id, {"query_ids": ["query-a"]}, timeout=0.01)
    evidence = service.evidence(since=0, update_status=status)

    assert status == "data_received"
    assert evidence["queries"][0]["returned_rows"] == 1
    assert {check["code"] for check in evidence["result_checks"]} == {issue}
    assert all(check["status"] == "inconclusive" for check in evidence["result_checks"])


@pytest.mark.asyncio
async def test_open_date_range_bound_does_not_create_a_range_warning():
    service = _preview_service()
    service.queries = [{"id": "query-a", "parameters": [{"name": "period", "type": "date_range"}]}]
    action_id = service.begin_action()
    await _complete_query(
        service,
        action_id,
        applied_params={"period": {"from": "2024-06-01"}},
        data={"rows": [{"id": 1}]},
    )

    status = await service.wait(action_id, {"query_ids": ["query-a"]}, timeout=0.01)
    evidence = service.evidence(since=0, update_status=status)

    assert status == "data_received"
    assert evidence["result_checks"] == []


class _GetResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload


class _GetClient:
    def __init__(self, status_code, payload=None):
        self.response = _GetResponse(status_code, payload)

    async def get(self, path):
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404])
async def test_preview_recheck_classifies_auth_and_missing_artifacts_as_restricted(status_code):
    from app.ai.tools.implementations._artifact_browser import run_artifact_operation
    from app.ai.tools.schemas.browser import BrowserSnapshotInput

    service = _preview_service()
    service.client = _GetClient(status_code)

    with pytest.raises(PermissionError):
        await run_artifact_operation(
            SimpleNamespace(preview=service), "snapshot", BrowserSnapshotInput(session_id="preview"), {}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_preview_recheck_classifies_server_failures_as_unavailable(status_code):
    from app.ai.tools.implementations._artifact_browser import run_artifact_operation
    from app.ai.tools.schemas.browser import BrowserSnapshotInput
    from app.services.artifact_preview_service import PreviewUnavailableError

    service = _preview_service()
    service.client = _GetClient(status_code)

    with pytest.raises(PreviewUnavailableError):
        await run_artifact_operation(
            SimpleNamespace(preview=service), "snapshot", BrowserSnapshotInput(session_id="preview"), {}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [True, False], ids=["restricted", "unavailable"])
async def test_failed_preview_recheck_never_returns_snapshot_or_evidence(restricted):
    from app.ai.tools.implementations._artifact_browser import artifact_operation_failure
    from app.services.artifact_preview_service import PreviewUnavailableError

    error_type = PermissionError if restricted else PreviewUnavailableError

    class DeniedFrame:
        def locator(self, selector):
            raise AssertionError("A failed access recheck must not inspect the page")

    class DeniedPreview:
        def identity(self):
            raise AssertionError("A failed access recheck must not read artifact metadata")

        def evidence(self, *args, **kwargs):
            raise AssertionError("A failed access recheck must not read stored evidence")

    session = SimpleNamespace(
        session_id="session-sensitive",
        preview=DeniedPreview(),
        frame=DeniedFrame(),
        action_lock=None,
    )
    result = await artifact_operation_failure(
        session, error_type("The access check failed"), {}
    )

    for payload in result.values():
        assert payload["success"] is False
        assert not {"snapshot", "evidence", "artifact", "parameters"} & payload.keys()
    assert result["output"]["error_code"] == ("preview_restricted" if restricted else "preview_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [(PermissionError, "preview_restricted"), (None, "preview_unavailable")],
)
async def test_preview_startup_access_failure_never_returns_evidence(monkeypatch, exception_type, expected_code):
    import app.services.artifact_preview_service as preview_module
    from app.ai.tools.implementations.browser_navigate import BrowserNavigateTool

    status_code = 403 if exception_type else 503
    clients = []

    class FailingClient:
        def __init__(self, **kwargs):
            self.closed = False
            clients.append(self)

        async def get(self, path):
            return _GetResponse(status_code)

        async def aclose(self):
            self.closed = True

    async def write_token(user):
        return "test-token"

    monkeypatch.setattr(preview_module.httpx, "AsyncClient", FailingClient)
    monkeypatch.setattr("app.core.auth.get_jwt_strategy", lambda: SimpleNamespace(write_token=write_token))
    events = [event async for event in BrowserNavigateTool().run_stream(
        {"artifact_id": "artifact-1"}, _preview_runtime_context()
    )]

    payload = events[-1].payload
    assert payload["output"]["success"] is False
    assert payload["output"]["error_code"] == expected_code
    assert all(payload["output"].get(key) is None for key in ("snapshot", "evidence", "artifact", "parameters"))
    assert all(payload["observation"].get(key) is None for key in ("snapshot", "evidence", "artifact", "parameters"))
    assert clients[0].closed is True


class _NoDialogFrame:
    def locator(self, selector):
        return self

    async def count(self):
        return 0


class _SnapshotPage:
    url = "https://preview.test/artifact-preview/artifact-1"


@pytest.mark.asyncio
async def test_bare_preview_snapshot_only_reports_undelivered_historical_errors(monkeypatch):
    from app.ai.tools.implementations import _artifact_browser as artifact_browser
    from app.ai.tools.schemas.browser import BrowserSnapshotInput

    service = _preview_service()
    service.artifact = {"id": "artifact-1", "version": 1, "content": {"code": ""}}
    service.url = "https://preview.test/artifact-preview/artifact-1"
    service.client = _GetClient(200, {"id": "artifact-1"})
    service.action_id = service.begin_action()
    service.record("error", source="runtime", message="old failure")
    # The prior tool response already delivered this event. A plain snapshot
    # rechecks current page state and must not turn the old failure into new output.
    service.evidence(since=0)

    async def snapshot(*args, **kwargs):
        return "- generic: current page", False

    monkeypatch.setattr(artifact_browser, "build_snapshot", snapshot)
    session = SimpleNamespace(
        preview=service,
        session_id="preview-session",
        frame=_NoDialogFrame(),
        page=_SnapshotPage(),
        action_lock=__import__("asyncio").Lock(),
    )

    result = await artifact_browser.run_artifact_operation(
        session, "snapshot", BrowserSnapshotInput(session_id=session.session_id), {}
    )

    assert result["output"]["evidence"]["errors"] == []
    assert result["output"]["evidence"]["update_status"] != "failed"


@pytest.mark.asyncio
async def test_explicit_preview_action_replay_still_includes_its_historical_error(monkeypatch):
    from app.ai.tools.implementations import _artifact_browser as artifact_browser
    from app.ai.tools.schemas.browser import BrowserSnapshotInput

    service = _preview_service()
    service.artifact = {"id": "artifact-1", "version": 1, "content": {"code": ""}}
    service.url = "https://preview.test/artifact-preview/artifact-1"
    service.client = _GetClient(200, {"id": "artifact-1"})
    action_id = service.begin_action()
    service.record("error", source="runtime", message="action failed")
    service.evidence(since=0)

    async def snapshot(*args, **kwargs):
        return "- generic: current page", False

    monkeypatch.setattr(artifact_browser, "build_snapshot", snapshot)
    session = SimpleNamespace(
        preview=service,
        session_id="preview-session",
        frame=_NoDialogFrame(),
        page=_SnapshotPage(),
        action_lock=__import__("asyncio").Lock(),
    )

    result = await artifact_browser.run_artifact_operation(
        session,
        "snapshot",
        BrowserSnapshotInput(session_id=session.session_id, evidence_for_action_id=action_id, since_cursor=0),
        {},
    )

    assert result["output"]["evidence"]["update_status"] == "failed"
    assert any(event["message"] == "action failed" for event in result["output"]["evidence"]["errors"])


@pytest.mark.asyncio
async def test_manager_returns_missing_stale_connector_id_so_navigation_can_open_fresh(monkeypatch):
    from app.ai.tools.implementations import browser_navigate
    from app.ai.tools.implementations.browser_navigate import BrowserNavigateTool
    import playwright.async_api
    from app.ai.tools.implementations._browser_common import BrowserSessionManager

    manager = BrowserSessionManager()
    monkeypatch.setattr(browser_navigate, "session_manager", manager)
    monkeypatch.setattr(browser_navigate, "build_snapshot", _fake_snapshot)
    monkeypatch.setattr(browser_navigate, "detect_block", _no_block)
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakePlaywrightFactory())
    ctx = _browser_runtime_context()
    events = [event async for event in BrowserNavigateTool().run_stream(
        {"url": "https://allowed.example.test/dashboard", "session_id": "stale-connector-session"}, ctx
    )]

    result = events[-1].payload
    assert result["output"]["success"] is True
    assert result["output"]["session_id"] != "stale-connector-session"
    assert manager.get(result["output"]["session_id"], ctx) is not None
    await manager.close_report("report-a")


@pytest.mark.asyncio
async def test_manager_strict_navigation_lookup_rejects_cross_scope_ids(monkeypatch):
    import playwright.async_api
    from app.ai.tools.implementations._browser_common import BrowserSessionManager

    manager = BrowserSessionManager()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakePlaywrightFactory())
    owner = {
        "organization": SimpleNamespace(id="tenant-a"),
        "user": SimpleNamespace(id="member-a"),
        "report": SimpleNamespace(id="report-a"),
        "agent_execution_id": "run-a",
    }
    other = {**owner, "user": SimpleNamespace(id="member-b"), "report": SimpleNamespace(id="report-b")}
    foreign = await manager.open("report-b", [], False, runtime_ctx=other)

    with pytest.raises(PermissionError):
        manager.get(foreign.session_id, owner, strict=True)
    await manager.close_report("report-b")


@pytest.mark.asyncio
async def test_navigation_rejects_preview_session_ids(monkeypatch):
    import playwright.async_api
    from app.ai.tools.implementations import browser_navigate
    from app.ai.tools.implementations.browser_navigate import BrowserNavigateTool
    from app.ai.tools.implementations._browser_common import BrowserSessionManager

    manager = BrowserSessionManager()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakePlaywrightFactory())
    ctx = _browser_runtime_context()

    class FakePreview:
        async def route(self, *args, **kwargs):
            return None

        async def close(self):
            return None

    preview = await manager.open("report-a", [], False, runtime_ctx=ctx, preview=FakePreview())
    monkeypatch.setattr(browser_navigate, "session_manager", manager)
    events = [event async for event in BrowserNavigateTool().run_stream(
        {"url": "https://allowed.example.test/dashboard", "session_id": preview.session_id}, ctx
    )]

    assert events[-1].payload["output"]["success"] is False
    assert events[-1].payload["output"]["error_code"] == "session_scope_mismatch"
    await manager.close_report("report-a")


class _FakePage:
    url = "about:blank"

    async def goto(self, url, **kwargs):
        self.url = url
        return SimpleNamespace(status=200)

    async def title(self):
        return "Allowed page"


def _browser_runtime_context():
    connection = SimpleNamespace(
        type="browser",
        config={"url_patterns": ["https://allowed.example.test/*"], "allow_downloads": False},
    )
    report = SimpleNamespace(
        id="report-a",
        data_sources=[SimpleNamespace(connections=[connection])],
    )
    return {
        "organization": SimpleNamespace(id="tenant-a"),
        "user": SimpleNamespace(id="member-a"),
        "report": report,
        "agent_execution_id": "run-a",
    }


async def _fake_snapshot(*args, **kwargs):
    return "- heading \"Allowed page\"", False


async def _no_block(*args, **kwargs):
    return None


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()
        self.download_handler = None

    async def new_page(self):
        return self.page

    async def route(self, *args, **kwargs):
        return None

    async def route_web_socket(self, *args, **kwargs):
        return None

    async def close(self):
        return None


class _FakeBrowser:
    async def new_context(self, **kwargs):
        return _FakeContext()

    async def close(self):
        return None


class _FakeChromium:
    async def launch(self, **kwargs):
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()

    async def stop(self):
        return None


class _FakePlaywrightFactory:
    async def start(self):
        return _FakePlaywright()


@pytest.mark.asyncio
async def test_capacity_reclaims_only_idle_connector_sessions(monkeypatch):
    import time
    import playwright.async_api
    from app.ai.tools.implementations._browser_common import (
        BrowserSessionManager,
    )

    manager = BrowserSessionManager()
    ctx = {
        "organization": SimpleNamespace(id="tenant-a"),
        "user": SimpleNamespace(id="member-new"),
        "report": SimpleNamespace(id="report-new"),
        "agent_execution_id": "run-new",
    }
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakePlaywrightFactory())
    monkeypatch.setattr("app.ai.tools.implementations._browser_common.MAX_CONCURRENT_SESSIONS", 3)
    idle_ctx = {
        "organization": SimpleNamespace(id="tenant-old"),
        "user": SimpleNamespace(id="member-old"),
        "report": SimpleNamespace(id="report-old"),
        "agent_execution_id": "idle-run",
    }
    idle = await manager.open("report-old", [], False, runtime_ctx=idle_ctx)
    await manager.close_execution("idle-run")
    idle.last_used = time.monotonic() - 10
    active_ctx = {
        "organization": SimpleNamespace(id="tenant-a"),
        "user": SimpleNamespace(id="member-active"),
        "report": SimpleNamespace(id="report-active"),
        "agent_execution_id": "active-run",
    }
    active = await manager.open("report-active", [], False, runtime_ctx=active_ctx)
    active.last_used = time.monotonic() - 20
    class FakePreview:
        async def route(self, *args, **kwargs):
            return None

        async def close(self):
            return None

    preview_ctx = {
        "organization": SimpleNamespace(id="tenant-a"),
        "user": SimpleNamespace(id="member-preview"),
        "report": SimpleNamespace(id="report-preview"),
        "agent_execution_id": "preview-run",
    }
    preview = await manager.open("report-preview", [], False, runtime_ctx=preview_ctx, preview=FakePreview())
    preview.last_used = time.monotonic() - 30

    opened = await manager.open("report-new", ["https://example.test/*"], False, runtime_ctx=ctx)

    assert manager.get(idle.session_id, idle_ctx) is None
    assert manager.get(active.session_id, active_ctx) is active
    assert manager.get(preview.session_id, preview_ctx) is preview
    assert manager.get(opened.session_id, ctx) is opened
    await manager.close_report("report-new")
    await manager.close_report("report-active")
    await manager.close_report("report-preview")


@pytest.mark.asyncio
async def test_connector_session_tracks_multiple_live_executions_independently(monkeypatch):
    import playwright.async_api
    from app.ai.tools.implementations._browser_common import BrowserSessionManager

    manager = BrowserSessionManager()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakePlaywrightFactory())
    first_ctx = _browser_runtime_context()
    second_ctx = {**first_ctx, "agent_execution_id": "run-b"}

    first = await manager.open("report-a", ["https://allowed.example.test/*"], False, runtime_ctx=first_ctx)
    second = await manager.open("report-a", ["https://allowed.example.test/*"], False, runtime_ctx=second_ctx)

    assert second is first
    assert first.active_execution_ids == {"run-a", "run-b"}
    await manager.close_execution("run-a")
    assert first.active_execution_ids == {"run-b"}
    assert manager.get(first.session_id, second_ctx) is first
    await manager.close_execution("run-b")
    assert first.active_execution_ids == set()
    assert manager.get(first.session_id, second_ctx) is first
    await manager.close_report("report-a")


@pytest.mark.asyncio
async def test_full_capacity_of_active_sessions_is_preserved_and_new_open_fails(monkeypatch):
    import time
    import playwright.async_api
    from app.ai.tools.implementations._browser_common import BrowserSessionManager

    manager = BrowserSessionManager()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakePlaywrightFactory())
    monkeypatch.setattr("app.ai.tools.implementations._browser_common.MAX_CONCURRENT_SESSIONS", 3)
    contexts = []
    sessions = []
    for index in range(3):
        ctx = {
            "organization": SimpleNamespace(id="tenant-capacity"),
            "user": SimpleNamespace(id=f"member-{index}"),
            "report": SimpleNamespace(id=f"report-{index}"),
            "agent_execution_id": f"run-{index}",
        }
        contexts.append(ctx)
        sessions.append(await manager.open(str(ctx["report"].id), [], False, runtime_ctx=ctx))
        sessions[-1].last_used = time.monotonic() - 3600

    with pytest.raises(RuntimeError):
        await manager.open("report-over-capacity", [], False, runtime_ctx={
            "organization": SimpleNamespace(id="tenant-capacity"),
            "user": SimpleNamespace(id="member-new"),
            "report": SimpleNamespace(id="report-new"),
            "agent_execution_id": "run-new",
        })

    assert all(manager.get(session.session_id, ctx) is session for session, ctx in zip(sessions, contexts))
    for session in sessions:
        await manager.close_report(session.scope[2])
