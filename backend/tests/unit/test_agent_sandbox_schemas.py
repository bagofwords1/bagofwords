"""API schemas for agent sandboxes never carry a secret.

Private keys, registry passwords, environment secret values and sandbox
tokens are write-only. These tests build each response schema from a model
holding recognisable secrets and look for them in the serialised output, so
they also catch a secret added to a model later and passed through.
"""
import uuid

import pytest
from pydantic import ValidationError

from app.models.sandbox import Sandbox, SandboxCompute, SandboxEnvironment, SandboxRun
from app.schemas.sandbox_schema import (
    SandboxComputeSchema,
    SandboxEnvironmentCreate,
    SandboxEnvironmentSchema,
    SandboxEnvironmentSummary,
    SandboxEnvironmentUpdate,
    SandboxRunSchema,
    SandboxSchema,
)


@pytest.fixture
def marker():
    return uuid.uuid4().hex


def _compute(marker):
    return SandboxCompute(
        id="b1", name="sbx-1", type="docker-ssh", config={"address": "host.example"},
        credentials={"ssh_private_key": f"KEY-{marker}", "one_time_password": ""},
        driver_state={"ssh_host_key": f"HOSTKEY-{marker}", "caddy_ca_cert": f"CA-{marker}"},
        capabilities=["stop_resume"], labels=["gpu"], capacity=20, enabled=True,
        bootstrap_status="ready", bootstrap_steps=[{"key": "docker", "title": "Check Docker", "status": "ok"}],
        status="healthy",
    )


def _environment(marker):
    return SandboxEnvironment(
        id="e1", name="Default", image="python:3.12-slim", registry_username="bot",
        registry_password=f"PW-{marker}", secrets={"API_KEY": f"SECRET-{marker}"},
        cpus=1.0, memory_mb=1024, disk_mb=1024, run_timeout_seconds=600, network_policy="none",
        allowed_domains=[], env_vars={"TZ": "UTC"}, compute_labels=[], ports=[],
        require_strong_isolation=False, is_default=True, enabled=True,
    )


def test_compute_response_names_stored_credentials_without_their_values(marker):
    schema = SandboxComputeSchema.from_model(
        _compute(marker), public_config={"address": "host.example"}, info={"isolation": "gvisor"},
    )
    dumped = schema.model_dump_json()
    assert marker not in dumped
    assert schema.credentials_set == ["ssh_private_key"]  # the empty one is not "set"


def test_environment_responses_return_secret_names_only(marker):
    env = _environment(marker)
    full = SandboxEnvironmentSchema.from_model(env)
    assert marker not in full.model_dump_json()
    assert full.secret_names == ["API_KEY"]
    assert full.has_registry_password is True
    assert full.env_vars == {"TZ": "UTC"}  # plain variables are not secrets
    # What the agent and non-admins see carries neither secrets nor their names.
    summary = SandboxEnvironmentSummary.from_model(env).model_dump_json()
    assert marker not in summary and "API_KEY" not in summary


def test_sandbox_response_has_no_token_and_no_driver_handle(marker):
    sandbox = Sandbox(
        id="s1", report_id="r1", state="ready", limits={"memory_mb": 1024},
        token=f"TOKEN-{marker}", driver_ref={"container": f"bow-sbx-{marker}", "ip": "10.0.0.9"},
    )
    dumped = SandboxSchema.from_model(sandbox).model_dump_json()
    assert marker not in dumped
    assert "10.0.0.9" not in dumped


def test_run_code_is_withheld_from_viewers_without_view_code(marker):
    run = SandboxRun(
        id="run1", sandbox_id="s1", report_id="r1", language="python", command=["python", "main.py"],
        code=f"print('{marker}')", code_sha256="ab" * 32, status="succeeded", exit_code=0,
        stdout="ok", stderr="", input_file_ids=[], artifact_file_ids=[],
    )
    hidden = SandboxRunSchema.from_model(run, include_code=False)
    assert hidden.code is None and hidden.command is None
    assert marker not in hidden.model_dump_json()
    assert hidden.code_sha256 == run.code_sha256  # the hash still identifies the run
    assert SandboxRunSchema.from_model(run, include_code=True).code == run.code


@pytest.mark.parametrize("field, bad", [
    ("env_vars", {"1BAD": "x"}),
    ("secrets", {"has space": "x"}),
    ("allowed_domains", ["https://pypi.org/simple"]),
    ("ports", [80]),
    ("network_policy", "open"),
    ("run_timeout_seconds", 99999),
])
def test_environment_input_is_validated(field, bad):
    with pytest.raises(ValidationError):
        SandboxEnvironmentCreate(name="e", image="img", **{field: bad})


def test_environment_defaults_are_the_safe_ones():
    env = SandboxEnvironmentCreate(name="e", image="img")
    assert env.network_policy == "none"
    assert env.require_strong_isolation is False
    assert SandboxEnvironmentCreate(name="e", image="img", allowed_domains=[" PyPI.org. ", "pypi.org"]).allowed_domains == ["pypi.org"]
    # An update leaves everything it does not mention untouched.
    assert SandboxEnvironmentUpdate().model_dump(exclude_unset=True) == {}
