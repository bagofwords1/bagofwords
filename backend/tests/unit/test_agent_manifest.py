"""Unit tests for the AgentManifest Pydantic surface.

These don't touch the DB — they just verify that the manifest accepts
valid shapes and rejects invalid ones with the right error codes.
"""

import pytest
from pydantic import ValidationError

from app.schemas.agent_manifest_schema import (
    AgentManifest,
    ApplyError,
    ApplyErrorCode,
    ApplyResult,
    ApplyStatus,
    ApplyWarning,
    ApplyWarningCode,
    MemberRef,
    TableRules,
    ToolsOverlay,
)


def test_minimal_manifest():
    m = AgentManifest(name="x")
    assert m.name == "x"
    assert m.connections == []
    assert m.tools == {}


def test_full_manifest_roundtrips():
    m = AgentManifest(
        name="analyst",
        description="hi",
        connections=["c1", "c2"],
        tables=TableRules(include=["c1.public.*"], exclude=["c1.public.staging_*"]),
        tools={"c1": ToolsOverlay(allow=["t1"], confirm=["t2"], deny=["t3"])},
        members=[
            MemberRef(user="a@b.com"),
            MemberRef(group="gtm", permissions=["manage"]),
        ],
        conversation_starters=["q1"],
    )
    dumped = m.model_dump(mode="json")
    again = AgentManifest.model_validate(dumped)
    assert again == m


def test_name_required_non_empty():
    with pytest.raises(ValidationError):
        AgentManifest(name="")
    with pytest.raises(ValidationError):
        AgentManifest(name="   ")


def test_duplicate_connections_rejected():
    with pytest.raises(ValidationError):
        AgentManifest(name="a", connections=["c1", "c1"])


def test_member_ref_requires_exactly_one_of():
    with pytest.raises(ValidationError):
        MemberRef()
    with pytest.raises(ValidationError):
        MemberRef(user="a@b.com", group="g")


def test_apply_result_error_envelope():
    r = ApplyResult(
        status=ApplyStatus.ERROR,
        errors=[
            ApplyError(
                loc=["connections", 0],
                code=ApplyErrorCode.CONNECTION_NOT_FOUND,
                message="missing",
                value="x",
                suggestion="y",
            )
        ],
    )
    j = r.model_dump(mode="json")
    assert j["status"] == "error"
    assert j["errors"][0]["code"] == "connection_not_found"
    assert j["errors"][0]["suggestion"] == "y"


def test_apply_result_success_envelope():
    r = ApplyResult(
        status=ApplyStatus.CREATED,
        id="id",
        name="n",
        warnings=[
            ApplyWarning(
                code=ApplyWarningCode.CONNECTION_INDEXING_PENDING,
                message="still indexing",
            )
        ],
    )
    j = r.model_dump(mode="json")
    assert j["status"] == "created"
    assert j["warnings"][0]["code"] == "connection_indexing_pending"
    assert j["errors"] == []
