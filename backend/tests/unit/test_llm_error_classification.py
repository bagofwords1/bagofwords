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


# ── context_length coverage across providers ──────────────────────────────
# The overflow remediation (shrink trim budget, forced compaction, window-
# aware fallback) triggers ONLY on code == context_length, so every
# provider's real overflow wording must land there — in both the typed-
# exception form (loop rescue) and the stringified form (planner path).

def test_anthropic_overflow_string_classifies_as_context_length():
    body = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
            "'message': 'prompt is too long: 250000 tokens > 200000 maximum'}}")
    assert _code("anthropic", body) == "context_length"


def test_openai_overflow_string_with_apostrophe_classifies_as_context_length():
    """repr() puts apostrophe-containing text in double quotes; the message
    extractor used to truncate at \"This model\" and the marker never hit."""
    body = ("Error code: 400 - {'error': {'message': \"This model's maximum context length "
            "is 128000 tokens. However, your messages resulted in 130000 tokens.\", "
            "'code': 'context_length_exceeded'}}")
    assert _code("openai", body) == "context_length"


def test_gemini_overflow_string_classifies_as_context_length():
    body = ("400 INVALID_ARGUMENT. The input token count (1200000) exceeds the maximum "
            "number of tokens allowed (1048576).")
    assert _code("google", body) == "context_length"


def test_bedrock_overflow_string_classifies_as_context_length():
    """Stringified botocore carries no numeric status; ValidationException
    must map to 400 for the context_length branch to see it at all."""
    body = ("An error occurred (ValidationException) when calling the Converse operation: "
            "The model returned the following errors: prompt is too long: 219922 tokens > 204698 maximum")
    assert classify(Exception(body), provider="bedrock", model="m").code == "context_length"


def test_bedrock_legacy_overflow_wording_classifies_as_context_length():
    body = ("An error occurred (ValidationException) when calling the InvokeModelWithResponseStream "
            "operation: Input is too long for requested model.")
    assert classify(Exception(body), provider="bedrock", model="m").code == "context_length"


def test_bedrock_overflow_object_classifies_as_context_length():
    class _ClientError(Exception):
        response = {"ResponseMetadata": {"HTTPStatusCode": 400}}

    err = classify(
        _ClientError("An error occurred (ValidationException) when calling Converse: "
                     "Input is too long for requested model."),
        provider="bedrock",
        model="m",
    )
    assert err.status == 400
    assert err.code == "context_length"


def test_provider_message_survives_embedded_apostrophe():
    from app.ai.llm.errors import _extract_provider_message
    raw = ("Error code: 400 - {'error': {'message': \"This model's maximum context length "
           "is 128000 tokens.\", 'code': 'x'}}")
    msg = _extract_provider_message(Exception(raw), raw)
    assert "maximum context length" in msg


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


class TestModelNotFound:
    """A missing deployment/route is permanent, so it must not be retried.

    Reported as an Azure AI Foundry Claude deployment that appeared to hang:
    the endpoint answered 404 in ~200ms, but 'provider_error' is retryable, so
    the LLM facade, the planner and the fallback chain each replayed it with
    backoff. The user saw ~46s of "thinking" and no error at all.
    """

    @staticmethod
    def _err(body, status=404, cls_name="NotFoundError"):
        exc = type(cls_name, (Exception,), {})(f"Error code: {status} - {body}")
        exc.status_code = status
        return exc

    @pytest.mark.parametrize("body", [
        # Azure AI Foundry, OpenAI-compatible surface asked for a Claude model
        {"error": {"code": "api_not_supported", "message": "Requested API is currently not supported"}},
        # Azure OpenAI, deployment name that does not exist
        {"error": {"code": "DeploymentNotFound",
                   "message": "The API deployment for this resource does not exist."}},
        # OpenAI proper
        {"error": {"code": "model_not_found",
                   "message": "The model 'x' does not exist or you do not have access to it."}},
    ])
    def test_classified_as_model_not_found(self, body):
        err = classify(self._err(body), provider="azure", model="claude-haiku-4-5")
        assert err.code == "model_not_found"
        assert err.status == 404

    def test_is_not_retryable(self):
        from app.ai.llm.llm import _RETRYABLE_CODES
        assert "model_not_found" not in _RETRYABLE_CODES

    def test_is_fallback_eligible_at_model_scope(self):
        # Another model is the only way to serve the run, but one missing
        # deployment says nothing about the rest of the endpoint.
        from app.ai.llm.fallback import (
            FALLBACK_ELIGIBLE_CODES, PROVIDER_SCOPE_CODES, STICKY_CODES,
        )
        assert "model_not_found" in FALLBACK_ELIGIBLE_CODES
        assert "model_not_found" not in PROVIDER_SCOPE_CODES
        assert "model_not_found" in STICKY_CODES

    def test_provider_message_is_preserved(self):
        body = {"error": {"code": "api_not_supported", "message": "Requested API is currently not supported"}}
        err = classify(self._err(body), provider="azure", model="claude-haiku-4-5")
        assert "not supported" in err.provider_message.lower()
        assert "claude-haiku-4-5" in err.summary

    def test_other_404s_without_markers_stay_provider_error(self):
        # Don't swallow every 404 — only ones that actually name a missing model.
        exc = Exception("Error code: 404 - {'error': {'message': 'Resource not found'}}")
        exc.status_code = 404
        assert classify(exc, provider="azure", model="m").code == "provider_error"

    def test_5xx_and_429_are_unchanged(self):
        for status, expected in ((500, "provider_error"), (503, "provider_error")):
            exc = Exception(f"Error code: {status} - server error")
            exc.status_code = status
            assert classify(exc, provider="azure", model="m").code == expected
