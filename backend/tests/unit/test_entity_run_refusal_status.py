"""The status an entity run answers when its code was refused.

A refusal raised in the parent reaches the route as its own type; one raised
while the sandbox child ran the code reaches it as a SandboxExecutionError
naming the original type. Both must get the same answer: 429 with the
refusal's text for a quota or a connection rate limit, 503 with the operator
remedy when the host cannot provide the sandbox.
"""

import pytest

from app.ai.code_execution.sandbox.runner import (
    CONFINEMENT_REMEDY,
    SandboxExecutionError,
    SandboxUnavailableError,
)
from app.routes.entity import _refusal_http_error


@pytest.mark.parametrize("exc_type", ["UsageLimitExceeded", "RateLimitExceeded"])
def test_a_quota_or_rate_limit_raised_in_the_sandbox_answers_429_with_its_message(exc_type):
    message = f"{exc_type} for this connection: 7 of 7 used"
    answer = _refusal_http_error(SandboxExecutionError(exc_type=exc_type, message=message))
    assert answer is not None
    assert answer.status_code == 429
    assert answer.detail == message


def test_a_connection_with_no_free_slot_reported_by_the_sandbox_answers_429_try_again():
    """The child reports the parent's ConnectionBusyError by type name; its
    text (which names the connection) is replaced by a fixed one."""
    message = "Too many queries are already running against wh-secret (limit 4 concurrent)."
    answer = _refusal_http_error(SandboxExecutionError(exc_type="ConnectionBusyError", message=message))
    assert answer is not None
    assert answer.status_code == 429
    assert "Try again" in answer.detail and "wh-secret" not in answer.detail


def test_a_host_without_the_sandbox_answers_503_with_the_operator_remedy():
    answer = _refusal_http_error(SandboxUnavailableError("process creation restriction is unavailable"))
    assert answer is not None
    assert answer.status_code == 503
    assert "process creation restriction is unavailable" in answer.detail
    assert CONFINEMENT_REMEDY in answer.detail


@pytest.mark.parametrize("exc", [
    SandboxExecutionError(exc_type="KeyError", message="'month'"),
    ValueError("no rows"),
    None,
])
def test_an_ordinary_failure_is_not_a_refusal(exc):
    assert _refusal_http_error(exc) is None


@pytest.mark.db
def test_running_a_missing_entity_is_a_typed_not_found():
    """The service says the entity is missing with a typed 404, so the route
    never has to tell it from a failed run by the message text."""
    import asyncio
    import uuid
    from types import SimpleNamespace

    from app.dependencies import async_session_maker
    from app.errors import AppError
    from app.schemas.entity_schema import EntityRunPayload
    from app.services.entity_service import EntityService

    async def run_missing():
        async with async_session_maker() as db:
            await EntityService().run_entity_with_update(
                db, str(uuid.uuid4()), EntityRunPayload(), SimpleNamespace(id=str(uuid.uuid4())),
            )

    with pytest.raises(AppError) as raised:
        asyncio.run(run_missing())
    assert raised.value.status_code == 404
    assert raised.value.error_code == "entity.not_found"
