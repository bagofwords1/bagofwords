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
