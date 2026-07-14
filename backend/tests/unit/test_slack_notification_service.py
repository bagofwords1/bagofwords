import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import slack_notification_service as svc


def _make_step(data_type, n_rows):
    rows = [{"name": f"r{i}", "value": i} for i in range(n_rows)]
    columns = [
        {"field": "name", "headerName": "Name"},
        {"field": "value", "headerName": "Value"},
    ]
    return SimpleNamespace(
        id="step-1",
        title="My Table",
        data_model={"type": data_type},
        data={"rows": rows, "columns": columns},
    )


def test_step_row_count():
    assert svc._step_row_count(_make_step("table", 0)) == 0
    assert svc._step_row_count(_make_step("table", 5)) == 5
    assert svc._step_row_count(SimpleNamespace(data=None)) == 0


def _patch_session_returning(step, platform):
    """Return a context-manager session whose execute() yields step then platform."""
    db = AsyncMock()

    step_result = MagicMock()
    step_result.scalar_one_or_none.return_value = step
    platform_result = MagicMock()
    platform_result.scalar_one_or_none.return_value = platform

    db.execute = AsyncMock(side_effect=[step_result, platform_result])

    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=db)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    session_maker = MagicMock(return_value=session_ctx)
    return session_maker


@pytest.mark.asyncio
@pytest.mark.parametrize("n_rows", [0, 1, 9])
async def test_small_table_is_not_sent(n_rows):
    step = _make_step("table", n_rows)
    platform = SimpleNamespace(platform_type="slack", organization_id="org-1")
    adapter = AsyncMock()

    with (
        patch.object(svc, "create_async_session_factory", return_value=_patch_session_returning(step, platform)),
        patch.object(svc.PlatformAdapterFactory, "create_adapter", return_value=adapter),
    ):
        await svc.send_step_result_to_slack(
            step_id="step-1",
            external_user_id="U1",
            organization_id="org-1",
            platform_type="slack",
        )

    adapter.send_dm_in_thread.assert_not_called()
    adapter.send_file_in_thread.assert_not_called()


@pytest.mark.asyncio
async def test_large_table_is_sent():
    step = _make_step("table", 10)
    platform = SimpleNamespace(platform_type="slack", organization_id="org-1")
    adapter = AsyncMock()
    adapter.send_file_in_thread = AsyncMock(return_value=True)

    with (
        patch.object(svc, "create_async_session_factory", return_value=_patch_session_returning(step, platform)),
        patch.object(svc.PlatformAdapterFactory, "create_adapter", return_value=adapter),
    ):
        await svc.send_step_result_to_slack(
            step_id="step-1",
            external_user_id="U1",
            organization_id="org-1",
            platform_type="slack",
        )

    # 10 rows >= threshold → CSV is uploaded
    adapter.send_file_in_thread.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("n_rows", [5, 10, 50])
async def test_teams_table_is_never_sent(n_rows):
    # Teams gets the data via the agent's text answer (which renders its own
    # markdown table), so the pipeline must not send a duplicate raw table —
    # regardless of row count.
    step = _make_step("table", n_rows)
    platform = SimpleNamespace(platform_type="teams", organization_id="org-1")
    adapter = AsyncMock()

    with (
        patch.object(svc, "create_async_session_factory", return_value=_patch_session_returning(step, platform)),
        patch.object(svc.PlatformAdapterFactory, "create_adapter", return_value=adapter),
    ):
        await svc.send_step_result_to_slack(
            step_id="step-1",
            external_user_id="U1",
            organization_id="org-1",
            platform_type="teams",
        )

    adapter.send_dm_in_thread.assert_not_called()
    adapter.send_file_in_thread.assert_not_called()


@pytest.mark.asyncio
async def test_small_count_is_still_sent():
    # Counts are scalar answers, not tabular data — the threshold must not apply.
    step = _make_step("count", 1)
    platform = SimpleNamespace(platform_type="slack", organization_id="org-1")
    adapter = AsyncMock()
    adapter.send_dm_in_thread = AsyncMock(return_value=True)

    with (
        patch.object(svc, "create_async_session_factory", return_value=_patch_session_returning(step, platform)),
        patch.object(svc.PlatformAdapterFactory, "create_adapter", return_value=adapter),
    ):
        await svc.send_step_result_to_slack(
            step_id="step-1",
            external_user_id="U1",
            organization_id="org-1",
            platform_type="slack",
        )

    adapter.send_dm_in_thread.assert_called_once()
