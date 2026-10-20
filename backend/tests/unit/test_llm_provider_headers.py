"""Unit tests for LLM provider custom headers + identity forwarding + Entra auth.

Covers the pure header-resolution module, the LLM facade wiring headers into
each client (verified on the constructed SDK clients — no network), and the
Azure Entra ID auth-mode plumbing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest
from cryptography.fernet import Fernet

from app.ai.llm.header_injection import (
    AUTO_AGENT_NAME,
    MAX_HEADER_VALUE_LEN,
    QuestionAgents,
    agent_name_for_report,
    build_provider_headers,
    reset_llm_identity,
    set_llm_identity,
    validate_header_config,
)
from app.ai.llm.llm import LLM
from app.services.mcp_context_injection import IdentityContext
from app.settings.config import settings


class FakeProvider(SimpleNamespace):
    """Duck-typed LLMProvider: enough surface for LLM.__init__."""

    def decrypt_credentials(self):
        fernet = Fernet(settings.bow_config.encryption_key)
        return (
            json.loads(fernet.decrypt(self.api_key.encode()).decode()),
            json.loads(fernet.decrypt(self.api_secret.encode()).decode()),
        )


def make_model(provider_type: str, additional_config: dict, api_key="sk-test", api_secret=None):
    fernet = Fernet(settings.bow_config.encryption_key)
    provider = FakeProvider(
        provider_type=provider_type,
        additional_config=additional_config,
        api_key=fernet.encrypt(json.dumps(api_key).encode()).decode(),
        api_secret=fernet.encrypt(json.dumps(api_secret).encode()).decode(),
    )
    return SimpleNamespace(
        model_id="test-model",
        provider=provider,
        organization_id="org-1",
        config=None,
        supports_vision=False,
    )


@pytest.fixture
def identity():
    token = set_llm_identity(
        IdentityContext(
            email="analyst@corp.com",
            name="Ada Analyst",
            user_id="u-123",
            role="admin",
            attributes={"department": "Finance", "employeeId": "E42"},
        )
    )
    yield
    reset_llm_identity(token)


# ---------------------------------------------------------------------------
# build_provider_headers
# ---------------------------------------------------------------------------

def test_static_headers_only():
    assert build_provider_headers({"headers": {"X-Team": "data"}}) == {"X-Team": "data"}


def test_injection_without_identity_is_omitted():
    cfg = {"header_injection": [{"header": "X-User-Email", "source": "user.email"}]}
    assert build_provider_headers(cfg) == {}


def test_injection_resolves_identity(identity):
    cfg = {
        "headers": {"X-Static": "s"},
        "header_injection": [
            {"header": "X-User-Email", "source": "user.email"},
            {"header": "X-Role", "source": "membership.role"},
            {"header": "X-Dept", "source": "membership.attr:department"},
            {"header": "X-Composite", "source": "static:corp\\{membership.attr:employeeId}"},
            {"header": "X-Missing", "source": "membership.attr:nope"},
        ],
    }
    assert build_provider_headers(cfg) == {
        "X-Static": "s",
        "X-User-Email": "analyst@corp.com",
        "X-Role": "admin",
        "X-Dept": "Finance",
        "X-Composite": "corp\\E42",
    }


def test_dynamic_wins_over_static(identity):
    cfg = {
        "headers": {"X-User-Email": "static@corp.com"},
        "header_injection": [{"header": "X-User-Email", "source": "user.email"}],
    }
    assert build_provider_headers(cfg) == {"X-User-Email": "analyst@corp.com"}


def test_header_value_newlines_stripped():
    token = set_llm_identity(IdentityContext(email="a@b.com\r\nX-Evil: 1"))
    try:
        cfg = {"header_injection": [{"header": "X-User-Email", "source": "user.email"}]}
        resolved = build_provider_headers(cfg)["X-User-Email"]
        assert "\r" not in resolved and "\n" not in resolved
        assert resolved.startswith("a@b.com")
    finally:
        reset_llm_identity(token)


def test_invalid_header_names_dropped_at_build():
    cfg = {"headers": {"Bad Name": "x", "X-Ok": "y"}}
    assert build_provider_headers(cfg) == {"X-Ok": "y"}


def test_empty_config():
    assert build_provider_headers(None) == {}
    assert build_provider_headers({}) == {}


# ---------------------------------------------------------------------------
# agent.name
# ---------------------------------------------------------------------------

def _report(*names, report_type="regular", focus=None):
    return SimpleNamespace(
        report_type=report_type,
        data_sources=[SimpleNamespace(id=f"ds-{n}", name=n) for n in names],
        focused_data_source_ids=focus,
    )


AGENT_CFG = {
    "header_injection": [
        {"header": "X-Agent", "source": "agent.name"},
        {"header": "X-Agent-Tagged", "source": "static:bow/{agent.name}"},
    ]
}


def _headers_for(agent_name: str) -> dict:
    token = set_llm_identity(IdentityContext(email="a@b.com", agent_name=agent_name))
    try:
        return build_provider_headers(AGENT_CFG)
    finally:
        reset_llm_identity(token)


@pytest.mark.parametrize("names", [("Sales",), ("Finance", "HR"), ("ops", "Billing", "crm")])
def test_agent_name_lists_every_attached_agent(names):
    value = agent_name_for_report(_report(*reversed(names)))
    sent = value.split(", ")
    assert sorted(sent) == sorted(names)
    # Stable regardless of attach order, case-insensitively by name.
    assert sent == sorted(names, key=str.casefold)
    assert agent_name_for_report(_report(*names)) == value


@pytest.mark.parametrize("report_type", ["regular", "scheduled"])
def test_agent_name_is_auto_when_no_agent_is_attached(report_type):
    assert agent_name_for_report(_report(report_type=report_type)) == AUTO_AGENT_NAME


@pytest.mark.parametrize("report", [None, _report(report_type="artifact_chat")])
def test_agent_name_is_empty_without_a_conversation_scope(report):
    assert agent_name_for_report(report) == ""


def test_auto_follow_up_starts_at_the_focused_agent():
    # Auto chat: nothing attached, but the first question's data tool focused
    # the report on an agent from the run's pool.
    pool = [SimpleNamespace(id="ds-sales", name="מכירות"), SimpleNamespace(id="ds-hr", name="HR")]
    assert agent_name_for_report(_report(focus=["ds-sales"]), pool) == "מכירות"


def test_explicit_focus_narrows_many_attached_agents():
    names = [f"Agent {i}" for i in range(12)]
    report = _report(*names, focus=["ds-Agent 3", "ds-Agent 7"])
    assert agent_name_for_report(report) == "Agent 3, Agent 7"


@pytest.mark.parametrize("focus", [["ds-gone"], []])
def test_unresolvable_or_empty_focus_falls_back(focus):
    assert agent_name_for_report(_report("Finance", "HR", focus=focus)) == "Finance, HR"
    assert agent_name_for_report(_report(focus=focus), []) == AUTO_AGENT_NAME


def test_agent_name_resolves_as_source_and_inside_static():
    assert _headers_for("Finance, HR") == {
        "X-Agent": "Finance, HR",
        "X-Agent-Tagged": "bow/Finance, HR",
    }


def test_agent_header_omitted_when_no_agent_stamped():
    headers = _headers_for("")
    assert "X-Agent" not in headers


# ---------------------------------------------------------------------------
# agent.name within a question: follows the agents its tools fetch from
# ---------------------------------------------------------------------------

SALES = SimpleNamespace(id="ds-sales", name="מכירות")
HR = SimpleNamespace(id="ds-hr", name="HR")
FIN = SimpleNamespace(id="ds-fin", name="Finance")
POOL = [SALES, HR, FIN]


def _data_call(*agents):
    return {
        "tables_by_source": [
            {"data_source_id": a.id, "tables": [{"name": "t"}]} for a in agents
        ]
    }


@pytest.mark.parametrize("start", [AUTO_AGENT_NAME, "HR, מכירות", ""])
def test_question_starts_at_the_chat_choice(start):
    assert QuestionAgents(start).value() == start


def test_question_value_accumulates_the_agents_its_tools_name():
    q = QuestionAgents(AUTO_AGENT_NAME)
    assert q.add_tool("create_data", _data_call(SALES), POOL) is True
    assert q.value() == "מכירות"
    assert q.add_tool("create_data", _data_call(HR), POOL) is True
    assert sorted(q.value().split(", ")) == sorted(["HR", "מכירות"])
    # Using an agent again changes nothing.
    assert q.add_tool("create_data", _data_call(SALES, HR), POOL) is False


def test_one_call_naming_several_agents_counts_them_all():
    q = QuestionAgents(AUTO_AGENT_NAME)
    q.add_tool("create_data", _data_call(FIN, HR), POOL)
    assert sorted(q.value().split(", ")) == ["Finance", "HR"]


def test_agents_found_while_a_tool_runs_count():
    # File tools resolve their agent during execution (used_agent_ids).
    q = QuestionAgents(AUTO_AGENT_NAME)
    assert q.add([HR.id], POOL) is True
    assert q.value() == "HR"


@pytest.mark.parametrize(
    "tool, tool_input",
    [
        ("run_query", {"query_id": "q-1"}),  # a saved query names no agent
        ("execute_mcp", {"connection_id": SALES.id, "tool_name": "x"}),
        ("describe_tables", {"query": "orders", "data_source_ids": []}),  # = every agent
        ("describe_tables", {"query": "orders"}),
        # Same field, but attaching an eval to an agent is not using it.
        ("create_eval", {"prompt": "p", "data_source_ids": [HR.id]}),
        ("create_data", {"tables_by_source": [{"data_source_id": None, "tables": []}]}),
        ("create_data", _data_call(SimpleNamespace(id="ds-unknown", name="Ghost"))),
        ("create_data", None),
    ],
)
@pytest.mark.parametrize("start", [AUTO_AGENT_NAME, "HR, מכירות"])
def test_a_tool_that_names_no_known_agent_leaves_the_value(tool, tool_input, start):
    q = QuestionAgents(start)
    assert q.add_tool(tool, tool_input, POOL) is False
    assert q.value() == start
    q.add_tool("create_data", _data_call(FIN), POOL)
    assert q.add_tool(tool, tool_input, POOL) is False
    assert q.value() == "Finance"


def _described(*agents):
    return {"top_tables": [{"data_source_id": a.id, "name": "t"} for a in agents]}


@pytest.mark.parametrize("tool_input", [{"query": ["orders_1", "orders_2"]}, {"query": "x", "data_source_ids": []}])
def test_describe_tables_without_named_agents_counts_the_agents_it_found(tool_input):
    q = QuestionAgents(AUTO_AGENT_NAME)
    assert q.add_tool("describe_tables", tool_input, POOL, _described(SALES, HR, SALES)) is True
    assert sorted(q.value().split(", ")) == ["HR", "מכירות"]


@pytest.mark.parametrize("tool_output", [None, {"top_tables": []}, {"error": "boom"}])
def test_describe_tables_that_found_nothing_leaves_the_value(tool_output):
    q = QuestionAgents(AUTO_AGENT_NAME)
    assert q.add_tool("describe_tables", {"query": ["x"]}, POOL, tool_output) is False
    assert q.value() == AUTO_AGENT_NAME


def test_only_describe_tables_output_is_read():
    q = QuestionAgents(AUTO_AGENT_NAME)
    assert q.add_tool("search_agents", {"query": ["orders"]}, POOL, _described(SALES)) is False
    assert q.value() == AUTO_AGENT_NAME


def test_describe_tables_counts_the_agents_it_names():
    q = QuestionAgents(AUTO_AGENT_NAME)
    assert q.add_tool("describe_tables", {"query": "orders", "data_source_ids": [SALES.id, HR.id]}, POOL) is True
    assert sorted(q.value().split(", ")) == ["HR", "מכירות"]
    during = q.value_during("describe_tables", {"query": "x", "data_source_ids": [FIN.id]}, POOL)
    assert sorted(during.split(", ")) == ["Finance", "HR", "מכירות"]


def test_value_during_a_tool_is_scoped_to_that_tool():
    q = QuestionAgents(AUTO_AGENT_NAME)
    q.add_tool("create_data", _data_call(SALES), POOL)
    # Two tools running in parallel each see the question's agents plus
    # their own — never the sibling's.
    during_hr = q.value_during("create_data", _data_call(HR), POOL)
    during_fin = q.value_during("create_data", _data_call(FIN), POOL)
    assert sorted(during_hr.split(", ")) == ["HR", "מכירות"]
    assert sorted(during_fin.split(", ")) == ["Finance", "מכירות"]
    assert q.value() == "מכירות"


def test_value_during_a_tool_that_names_no_agent_is_the_question_value():
    q = QuestionAgents(AUTO_AGENT_NAME)
    assert q.value_during("run_query", {"query_id": "q-1"}, POOL) == AUTO_AGENT_NAME


def test_question_value_reaches_the_header():
    q = QuestionAgents(AUTO_AGENT_NAME)
    assert _headers_for(q.value())["X-Agent"] == AUTO_AGENT_NAME
    q.add_tool("create_data", _data_call(HR), POOL)
    assert _headers_for(q.value()) == {"X-Agent": "HR", "X-Agent-Tagged": "bow/HR"}


# ---------------------------------------------------------------------------
# Non-ASCII values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["מכירות", "Ventas – España", "דוח %41 Q3", "財務"])
def test_non_ascii_value_is_sendable_and_decodes_back(raw):
    value = _headers_for(raw)["X-Agent"]
    assert value.isascii()
    assert unquote(value) == raw
    # httpx (under every OpenAI-family / Anthropic client) must accept it.
    httpx.Request("POST", "http://gateway/v1/chat/completions", headers={"X-Agent": value})


@pytest.mark.parametrize("raw", ["Sales (EU)", "ops-team_2", "analyst+bi@corp.com", "corp\\E42"])
def test_ascii_value_without_percent_is_sent_unchanged(raw):
    assert _headers_for(raw)["X-Agent"] == raw


@pytest.mark.parametrize("raw", ["Sales 100%", "a%20b", "50% (EU)"])
def test_percent_is_escaped_so_every_value_decodes_back(raw):
    value = _headers_for(raw)["X-Agent"]
    assert "%25" in value
    assert unquote(value) == raw


@pytest.mark.parametrize("raw", ["Sales 100%", "ייצור", "a%20b"])
def test_static_value_is_stored_as_typed_and_encoded_once_when_sent(raw):
    headers, _ = validate_header_config({"X-Env": raw}, None)
    assert headers["X-Env"] == raw
    assert unquote(build_provider_headers({"headers": headers})["X-Env"]) == raw


def test_non_ascii_static_header_is_sendable():
    value = build_provider_headers({"headers": {"X-Env": "ייצור"}})["X-Env"]
    assert value.isascii() and unquote(value) == "ייצור"


def test_long_non_ascii_value_is_capped_without_a_broken_escape():
    value = _headers_for("א" * 2000)["X-Agent"]
    assert len(value) <= MAX_HEADER_VALUE_LEN
    assert set(unquote(value, errors="strict")) == {"א"}


# ---------------------------------------------------------------------------
# validate_header_config (storage-time validation)
# ---------------------------------------------------------------------------

def test_validate_normalizes():
    headers, rules = validate_header_config(
        {"X-Ok": " v ", "": "skip", "X-Empty": ""},
        [{"header": "X-U", "source": "user.email"}, {"header": "", "source": ""}],
    )
    assert headers == {"X-Ok": "v"}
    assert rules == [{"header": "X-U", "source": "user.email"}]


def test_validate_rejects_bad_name():
    with pytest.raises(ValueError, match="Invalid header name"):
        validate_header_config({"Bad Name": "1"}, None)
    with pytest.raises(ValueError, match="Invalid header name"):
        validate_header_config(None, [{"header": "X:Y", "source": "user.email"}])


def test_validate_rejects_missing_source():
    with pytest.raises(ValueError, match="no source"):
        validate_header_config(None, [{"header": "X-U", "source": ""}])


def test_validate_rejects_too_many():
    with pytest.raises(ValueError, match="Too many"):
        validate_header_config({f"X-{i}": "v" for i in range(30)}, None)


# ---------------------------------------------------------------------------
# LLM facade → client wiring
# ---------------------------------------------------------------------------

HEADER_CFG = {
    "headers": {"X-Static": "s"},
    "header_injection": [
        {"header": "X-User-Email", "source": "user.email"},
        {"header": "X-Agent", "source": "agent.name"},
    ],
}

# Every provider family whose SDK sends through httpx; extra config per family.
HTTPX_PROVIDERS = [
    ("custom", {"base_url": "http://gateway.test/v1"}),
    ("openai", {}),
    ("anthropic", {}),
    ("azure", {"endpoint_url": "https://res.openai.azure.com"}),
    ("google", {}),
]


@pytest.fixture
def sent(monkeypatch):
    """Requests leaving any httpx client, captured at the network boundary."""
    seen = []

    def handle(self, request):
        seen.append(request.headers)
        return httpx.Response(200, json={"data": [], "models": []}, request=request)

    async def handle_async(self, request):
        return handle(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async)
    # Admin-configured gateways go through the pinned egress transport, which
    # resolves the host before sending; give the fake hosts a public address.
    from app.core import egress
    monkeypatch.setattr(egress, "_getaddrinfo", lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))])
    return seen


def _send_sync(llm, provider):
    if provider == "google":
        list(llm.client.client.models.list())
    else:
        llm.client.client.get("/models", cast_to=httpx.Response)


@pytest.fixture
def stamp():
    tokens = []

    def _stamp(**fields):
        tokens.append(set_llm_identity(IdentityContext(**fields)))

    yield _stamp
    for token in reversed(tokens):
        reset_llm_identity(token)


@pytest.mark.parametrize("provider, extra", HTTPX_PROVIDERS)
def test_identity_headers_resolve_when_each_request_is_sent(provider, extra, sent, stamp):
    stamp(email="analyst@corp.com", agent_name="auto")
    llm = LLM(make_model(provider, {**extra, **HEADER_CFG}))
    # The question's agent becomes known after the client was built.
    stamp(email="analyst@corp.com", agent_name="Finance")
    _send_sync(llm, provider)
    headers = sent[-1]
    assert headers["x-static"] == "s"
    assert headers["x-user-email"] == "analyst@corp.com"
    assert headers["x-agent"] == "Finance"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, extra", [p for p in HTTPX_PROVIDERS if p[0] != "google"])
async def test_async_clients_resolve_identity_per_request(provider, extra, sent, stamp):
    llm = LLM(make_model(provider, {**extra, **HEADER_CFG}))
    stamp(email="analyst@corp.com", agent_name="HR")
    await llm.client.async_client.get("/models", cast_to=httpx.Response)
    assert sent[-1]["x-agent"] == "HR"
    stamp(email="analyst@corp.com", agent_name="Finance, HR")
    await llm.client.async_client.get("/models", cast_to=httpx.Response)
    assert sent[-1]["x-agent"] == "Finance, HR"


def test_rule_without_a_value_is_omitted_per_request(sent, stamp):
    llm = LLM(make_model("custom", {"base_url": "http://gateway.test/v1", **HEADER_CFG}))
    stamp(email="analyst@corp.com")
    _send_sync(llm, "custom")
    assert "x-agent" not in sent[-1]
    assert sent[-1]["x-static"] == "s"


def test_no_headers_config_leaves_requests_clean(sent, stamp):
    stamp(email="analyst@corp.com", agent_name="Finance")
    llm = LLM(make_model("custom", {"base_url": "http://gateway.test/v1"}))
    _send_sync(llm, "custom")
    assert "x-static" not in sent[-1] and "x-agent" not in sent[-1]


@pytest.mark.asyncio
async def test_google_streaming_thread_keeps_the_identity(sent, stamp):
    # Gemini streaming collects chunks in an executor thread; the request it
    # sends must still carry the run's identity.
    from app.ai.llm.types import Message

    llm = LLM(make_model("google", dict(HEADER_CFG)))
    stamp(email="analyst@corp.com", agent_name="Finance")
    try:
        async for _ in llm.client.inference_stream_v2("gemini-2.5-flash", [Message(role="user", content="hi")]):
            pass
    except Exception:
        pass  # the stub response isn't a real stream; only the request matters
    assert sent and sent[-1]["x-agent"] == "Finance"


def _bedrock_llm(stamp):
    stamp(email="analyst@corp.com", agent_name="auto")
    return LLM(make_model(
        "bedrock", {"region": "us-east-1", "auth_mode": "api_key", **HEADER_CFG},
        api_key="bedrock-key",
    ))


def _emit_bedrock_request(llm, operation="Converse"):
    from botocore.awsrequest import AWSRequest

    request = AWSRequest(method="POST", url="https://bedrock-runtime.us-east-1.amazonaws.com/")
    llm.client.client.meta.events.emit(
        f"request-created.bedrock-runtime.{operation}", request=request, operation_name=operation
    )
    return request


def test_bedrock_headers_resolve_per_request(stamp):
    llm = _bedrock_llm(stamp)
    stamp(email="analyst@corp.com", agent_name="Finance")
    request = _emit_bedrock_request(llm)
    assert request.headers["X-Static"] == "s"
    assert request.headers["X-User-Email"] == "analyst@corp.com"
    assert request.headers["X-Agent"] == "Finance"
    # Bearer auth from api_key mode must survive alongside custom headers.
    assert request.headers["Authorization"] == "Bearer bedrock-key"


@pytest.mark.asyncio
async def test_bedrock_streaming_thread_keeps_the_identity(stamp, monkeypatch):
    # Streaming runs boto3 in an executor thread; the request it sends must
    # still carry the run's identity.
    llm = _bedrock_llm(stamp)
    stamp(email="analyst@corp.com", agent_name="Finance")
    captured = {}

    def fake_converse_stream(**kwargs):
        captured["request"] = _emit_bedrock_request(llm, "ConverseStream")
        return {"stream": []}

    monkeypatch.setattr(llm.client.client, "converse_stream", fake_converse_stream)
    async for _ in llm.client.inference_stream("model-id", "hi"):
        pass
    assert captured["request"].headers["X-Agent"] == "Finance"


# ---------------------------------------------------------------------------
# Azure Entra ID auth modes
# ---------------------------------------------------------------------------

def test_azure_entra_client_secret_builds_token_provider(identity):
    model = make_model(
        "azure",
        {
            "endpoint_url": "https://res.openai.azure.com",
            "auth_mode": "entra_client_secret",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "client_id": "22222222-2222-2222-2222-222222222222",
            **HEADER_CFG,
        },
        api_key=None,
        api_secret="entra-client-secret",
    )
    llm = LLM(model)
    from app.ai.llm.clients.azure_client import AzureClient

    assert isinstance(llm.client, AzureClient)
    # Static headers ride on the client; identity rules resolve per request
    # (see test_identity_headers_resolve_when_each_request_is_sent).
    assert dict(llm.client.client.default_headers)["X-Static"] == "s"


def test_azure_entra_client_secret_requires_fields():
    model = make_model(
        "azure",
        {
            "endpoint_url": "https://res.openai.azure.com",
            "auth_mode": "entra_client_secret",
            "tenant_id": "t",
            # client_id missing
        },
        api_key=None,
        api_secret="secret",
    )
    with pytest.raises(ValueError, match="entra_client_secret"):
        LLM(model)


def test_azure_entra_default_no_stored_credentials():
    fernet_free = FakeProvider(
        provider_type="azure",
        additional_config={
            "endpoint_url": "https://res.openai.azure.com",
            "auth_mode": "entra_default",
        },
        api_key=None,
        api_secret=None,
    )
    model = SimpleNamespace(
        model_id="gpt-4o", provider=fernet_free, organization_id="org-1",
        config=None, supports_vision=False,
    )
    llm = LLM(model)  # must not raise on decrypt failure
    from app.ai.llm.clients.azure_client import AzureClient

    assert isinstance(llm.client, AzureClient)


def test_azure_entra_ignores_responses_api(identity):
    model = make_model(
        "azure",
        {
            "endpoint_url": "https://res.openai.azure.com",
            "auth_mode": "entra_client_secret",
            "tenant_id": "t",
            "client_id": "c",
            "use_responses_api": True,
        },
        api_key=None,
        api_secret="secret",
    )
    llm = LLM(model)
    from app.ai.llm.clients.azure_client import AzureClient

    assert isinstance(llm.client, AzureClient)
