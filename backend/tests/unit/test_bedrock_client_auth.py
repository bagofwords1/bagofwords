"""Unit tests for BedrockClient auth modes.

Exercises client construction only — no AWS calls are made. The api_key mode
is verified by emitting a synthetic request-created event and checking that
the Bearer header lands on the request, and that SigV4 signing is disabled.
"""

from __future__ import annotations

import pytest
from botocore import UNSIGNED
from botocore.awsrequest import AWSRequest

from app.ai.llm.clients.bedrock_client import BedrockClient

_REGION = "eu-west-1"


def test_unsupported_auth_mode_rejected():
    with pytest.raises(ValueError, match="Unsupported auth_mode"):
        BedrockClient(region=_REGION, auth_mode="oauth")


def test_iam_mode_constructs_client():
    client = BedrockClient(region=_REGION, auth_mode="iam")
    assert client.client.meta.region_name == _REGION


def test_access_keys_mode_requires_both_keys():
    with pytest.raises(ValueError, match="access_keys"):
        BedrockClient(region=_REGION, auth_mode="access_keys", aws_access_key_id="AKIA123")


def test_api_key_mode_requires_key():
    with pytest.raises(ValueError, match="api_key"):
        BedrockClient(region=_REGION, auth_mode="api_key")


def test_api_key_mode_disables_sigv4():
    client = BedrockClient(region=_REGION, auth_mode="api_key", api_key="bedrock-api-key-test")
    assert client.client.meta.config.signature_version is UNSIGNED


def test_api_key_mode_injects_bearer_header():
    api_key = "bedrock-api-key-test"
    client = BedrockClient(region=_REGION, auth_mode="api_key", api_key=api_key)

    request = AWSRequest(method="POST", url="https://bedrock-runtime.eu-west-1.amazonaws.com/")
    client.client.meta.events.emit(
        "request-created.bedrock-runtime.Converse",
        request=request,
        operation_name="Converse",
    )

    assert request.headers["Authorization"] == f"Bearer {api_key}"


def test_api_key_stays_scoped_to_its_client():
    # Two providers in one process must not share credentials — the event
    # handler is registered per client instance, not process-globally.
    client_a = BedrockClient(region=_REGION, auth_mode="api_key", api_key="key-a")
    client_b = BedrockClient(region=_REGION, auth_mode="api_key", api_key="key-b")

    req_a = AWSRequest(method="POST", url="https://bedrock-runtime.eu-west-1.amazonaws.com/")
    req_b = AWSRequest(method="POST", url="https://bedrock-runtime.eu-west-1.amazonaws.com/")
    client_a.client.meta.events.emit(
        "request-created.bedrock-runtime.Converse", request=req_a, operation_name="Converse"
    )
    client_b.client.meta.events.emit(
        "request-created.bedrock-runtime.Converse", request=req_b, operation_name="Converse"
    )

    assert req_a.headers["Authorization"] == "Bearer key-a"
    assert req_b.headers["Authorization"] == "Bearer key-b"
