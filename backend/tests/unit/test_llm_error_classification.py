"""Classification of provider errors — with an emphasis on quota exhaustion.

The fallback chain decides whether to substitute a model purely from the code
``classify`` returns, and it does so from a *string*: planner_v3 wraps the
provider exception as ``PlannerError(message=str(exc))`` and agent_v2
re-classifies ``Exception(err_msg)``. So the cases below are written the way
they actually reach that decision — the wrapped RuntimeError text, not a live
SDK exception object.

The distinction that matters most here is quota vs rate_limit: both usually
arrive as a 429, but a rate limit heals in seconds (retry it) and an exhausted
allowance does not heal at all (switch providers, don't retry).
"""
import pytest

from app.ai.llm.errors import classify
from app.ai.llm.fallback import FALLBACK_ELIGIBLE_CODES
from app.ai.llm.llm import _RETRYABLE_CODES


def _wrapped(provider: str, body: str) -> Exception:
    """The exception shape the agent's fallback path actually classifies."""
    return Exception(
        f"LLM v2 streaming failed (provider={provider}, model=m): {body}"
    )


def _code(provider: str, body: str) -> str:
    return classify(_wrapped(provider, body), provider=provider, model="m").code


# ── quota / credit exhaustion, per provider ────────────────────────────────

QUOTA_CASES = [
    (
        "openai",
        "Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
        "please check your plan and billing details.', 'type': 'insufficient_quota', "
        "'code': 'insufficient_quota'}}",
    ),
    (
        "anthropic",
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API. Please "
        "go to Plans & Billing to upgrade or purchase credits.'}}",
    ),
    (
        "google",
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
        "current quota, please check your plan and billing details.', 'status': "
        "'RESOURCE_EXHAUSTED'}}",
    ),
    (
        "google",
        "400 FAILED_PRECONDITION. {'error': {'code': 400, 'message': 'Please enable "
        "billing on your project.', 'status': 'FAILED_PRECONDITION'}}",
    ),
    (
        "bedrock",
        "An error occurred (ServiceQuotaExceededException) when calling the Converse "
        "operation: Your account has exceeded the service quota for this model.",
    ),
    (
        "azure",
        "Error code: 429 - {'error': {'code': 'insufficient_quota', 'message': 'You have "
        "exceeded your assigned quota for this deployment.'}}",
    ),
    (
        "custom",  # OpenRouter / DeepSeek and other OpenAI-compatible endpoints
        "Error code: 402 - {'error': {'message': 'Insufficient credits. Add more using "
        "https://openrouter.ai/credits', 'code': 402}}",
    ),
]


@pytest.mark.parametrize("provider,body", QUOTA_CASES)
def test_quota_exhaustion_is_classified_as_quota(provider, body):
    assert _code(provider, body) == "quota"


def test_quota_is_fallback_eligible_but_never_retried():
    # The whole point: another provider is the only way to serve the run, and
    # retrying the exhausted one just adds backoff to a doomed call.
    assert "quota" in FALLBACK_ELIGIBLE_CODES
    assert "quota" not in _RETRYABLE_CODES


# ── rate limits stay rate limits ───────────────────────────────────────────

RATE_LIMIT_CASES = [
    (
        "openai",
        "Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-4o in "
        "organization org-x on tokens per min (TPM): Limit 30000.', 'code': "
        "'rate_limit_exceeded'}}",
    ),
    (
        "anthropic",
        "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error', "
        "'message': 'Number of request tokens has exceeded your per-minute rate limit'}}",
    ),
    (
        "azure",
        "Error code: 429 - {'error': {'code': '429', 'message': 'Requests to the "
        "ChatCompletions_Create Operation have exceeded token rate limit of your current "
        "OpenAI S0 pricing tier.'}}",
    ),
    (
        # Google reuses RESOURCE_EXHAUSTED and the word "quota" for plain
        # per-minute throttling — transient, and must not read as exhaustion.
        "google",
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': \"Quota exceeded for "
        "quota metric 'GenerateContent requests per minute'\", 'status': "
        "'RESOURCE_EXHAUSTED'}}",
    ),
    (
        "bedrock",
        "An error occurred (ThrottlingException) when calling the ConverseStream "
        "operation (reached max retries: 4): Too many requests, please wait before "
        "trying again.",
    ),
]


@pytest.mark.parametrize("provider,body", RATE_LIMIT_CASES)
def test_transient_throttling_is_classified_as_rate_limit(provider, body):
    assert _code(provider, body) == "rate_limit"


# ── our own usage caps are not provider quota ──────────────────────────────

@pytest.mark.parametrize(
    "detail",
    [
        "Monthly LLM token quota exceeded.",
        "Monthly LLM spend quota exceeded.",
        "Monthly usage quota exceeded.",
    ],
)
def test_internal_usage_limits_never_classify_as_quota(detail):
    """UsageLimitExceeded says "quota exceeded" too, and must not fall back.

    The provider is healthy; the org spent its own budget. Substituting a
    model would only spend it somewhere else, so these stay 'unknown' —
    surfaced verbatim, not retried, not eligible for substitution.
    """
    code = _code("openai", detail)
    assert code == "unknown"
    assert code not in FALLBACK_ELIGIBLE_CODES


# ── providers previously invisible to the classifier ───────────────────────

def test_bedrock_error_names_classify_without_an_http_status():
    """botocore keeps the status in a dict, so name-matching is the only signal.

    Before this, every Bedrock failure fell through to 'unknown' — neither
    retried by the façade nor eligible for fallback.
    """
    assert _code(
        "bedrock",
        "An error occurred (ModelNotReadyException) when calling the Converse "
        "operation: Model is not ready to serve inference requests.",
    ) == "provider_error"


def test_google_stringified_status_is_extracted():
    """google.genai renders as "503 UNAVAILABLE. {...}" — no "Error code:"."""
    err = classify(
        _wrapped(
            "google",
            "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'The model is "
            "overloaded. Please try again later.', 'status': 'UNAVAILABLE'}}",
        ),
        provider="google",
        model="m",
    )
    assert err.status == 503
    assert err.code == "provider_error"


def test_status_extraction_ignores_token_counts():
    """A bare "429 TPM"-style number must not be mistaken for a status."""
    err = classify(
        _wrapped("custom", "budget of 500 TPM reached for this deployment"),
        provider="custom",
        model="m",
    )
    assert err.status is None


def test_google_apierror_object_exposes_status_on_code():
    """Live google.genai APIError: int status on .code, string enum on .status."""

    class _APIError(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"

    err = classify(_APIError("resource exhausted"), provider="google", model="m")
    assert err.status == 429


def test_botocore_client_error_object_exposes_status_in_response_dict():
    class _ClientError(Exception):
        response = {"ResponseMetadata": {"HTTPStatusCode": 429}}

    err = classify(
        _ClientError("An error occurred (ThrottlingException) when calling Converse"),
        provider="bedrock",
        model="m",
    )
    assert err.status == 429
    assert err.code == "rate_limit"


# ── untouched classes ──────────────────────────────────────────────────────

def test_auth_and_context_length_still_win_over_quota():
    # An invalid key is not a billing problem, and neither is an over-long
    # prompt — both must keep their non-eligible codes.
    assert _code(
        "anthropic",
        "Error code: 401 - {'type': 'error', 'error': {'message': 'invalid x-api-key'}}",
    ) == "auth"
    assert _code(
        "anthropic",
        "Error code: 400 - {'type': 'error', 'error': {'message': 'prompt is too long: "
        "250000 tokens > maximum context length'}}",
    ) == "context_length"


def test_provider_message_is_preserved_on_quota():
    """The user sees what the provider actually said, never just our headline."""
    err = classify(
        _wrapped(
            "openai",
            "Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
            "please check your plan and billing details.', 'type': 'insufficient_quota'}}",
        ),
        provider="openai",
        model="m",
    )
    assert "exceeded your current quota" in err.provider_message
    assert err.summary and err.summary != err.provider_message
