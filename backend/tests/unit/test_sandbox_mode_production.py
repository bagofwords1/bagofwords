"""In-process code execution is for local development only.

Contract: with `BOW_CODE_SANDBOX=inprocess` and `ENVIRONMENT=production`,
generated code (data code and deck code alike) is refused before any of it
runs, with a terminal error that tells the operator to leave in-process mode
— not the kernel-confinement advice, which cannot fix it. Development keeps
in-process mode; production in subprocess mode is unaffected. Startup reports
the same problem once, with the remedy that matches it.
"""

from __future__ import annotations

import sys

import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.pptx_executor import PptxCodeExecutor
from app.ai.code_execution.sandbox import config, runner
from app.ai.code_execution.sandbox.runner import CONFINEMENT_REMEDY, SandboxExecutionError

_LINUX = sys.platform.startswith("linux")

def _environment(monkeypatch, environment: str, mode: str) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    monkeypatch.delenv("BOW_SANDBOX_REQUIRE_LANDLOCK", raising=False)


def _marker_code(marker) -> str:
    # Writes a file when it runs, so "refused before the code ran" is
    # observable from outside.
    return (
        "def generate_df(ds_clients, excel_files):\n"
        "    df = pd.DataFrame({'ran': [1]})\n"
        f"    df.to_csv({str(marker)!r})\n"
        "    return df\n"
    )


def _deck_code() -> str:
    return (
        "prs = Presentation()\n"
        "prs.slides.add_slide(prs.slide_layouts[5])\n"
        "prs.save(_pptx_output_path)\n"
    )


@pytest.mark.parametrize("environment", [
    "production", "PRODUCTION", "Production",
    # Anything that is not a known development name counts as production.
    " production", "production\n", "prod", "staging", "",
])
def test_production_refuses_in_process_code_before_it_runs(monkeypatch, tmp_path, environment):
    _environment(monkeypatch, environment, "inprocess")
    marker = tmp_path / "ran.csv"
    with pytest.raises(SandboxExecutionError) as refused:
        StreamingCodeExecutor().execute_code(code=_marker_code(marker), ds_clients={}, excel_files=[])
    assert not marker.exists()
    assert type(refused.value) is runner.SandboxModeRefused
    assert getattr(refused.value, "terminal_execution_error", False) is True
    assert runner.INPROCESS_REMEDY in str(refused.value)
    assert CONFINEMENT_REMEDY not in str(refused.value)


@pytest.mark.parametrize("environment", ["development", "dev", "test", "testing", " Development "])
def test_development_still_runs_code_in_process(monkeypatch, tmp_path, environment):
    _environment(monkeypatch, environment, "inprocess")
    marker = tmp_path / "ran.csv"
    df, _, _ = StreamingCodeExecutor().execute_code(code=_marker_code(marker), ds_clients={}, excel_files=[])
    assert list(df["ran"]) == [1]
    assert marker.exists()
    assert config.inprocess_refusal_reason() == ""


@pytest.mark.parametrize("environment,required", [
    ("production", True), (" production", True), ("prod", True), ("staging", True), ("", True),
    ("development", False), ("dev", False), ("test", False), ("testing", False),
])
def test_kernel_confinement_is_required_outside_development(monkeypatch, environment, required):
    _environment(monkeypatch, environment, "subprocess")
    assert config.SandboxLimits.from_env().require_landlock is required


@pytest.mark.skipif(not _LINUX, reason="sandbox child needs Linux")
def test_production_in_subprocess_mode_is_unaffected(monkeypatch, tmp_path):
    _environment(monkeypatch, "production", "subprocess")
    if runner.required_confinement_problem():
        pytest.skip("this host cannot provide the confinement production requires")
    code = "def generate_df(ds_clients, excel_files):\n    return pd.DataFrame({'ok': [1, 2]})\n"
    df, _, _ = StreamingCodeExecutor().execute_code(code=code, ds_clients={}, excel_files=[])
    assert list(df["ok"]) == [1, 2]
    assert config.inprocess_refusal_reason() == ""


def test_production_refuses_in_process_deck_code_before_it_runs(monkeypatch, tmp_path):
    _environment(monkeypatch, "production", "inprocess")
    out = tmp_path / "deck.pptx"
    with pytest.raises(SandboxExecutionError) as refused:
        PptxCodeExecutor().execute_pptx_code(
            code=_deck_code(), visualizations=[], report={}, output_path=out, images={},
        )
    assert not out.exists()
    assert type(refused.value) is runner.SandboxModeRefused
    assert runner.INPROCESS_REMEDY in str(refused.value)
    assert CONFINEMENT_REMEDY not in str(refused.value)


def test_development_still_builds_decks_in_process(monkeypatch, tmp_path):
    _environment(monkeypatch, "development", "inprocess")
    out = tmp_path / "deck.pptx"
    path, _ = PptxCodeExecutor().execute_pptx_code(
        code=_deck_code(), visualizations=[], report={}, output_path=out, images={},
    )
    assert path == out and out.exists()


# ---------------------------------------------------------------- startup

def _kernel(monkeypatch, abi: int, seccomp_reason: str = "") -> None:
    # The kernel probes are the boundary the startup check reads.
    from app.ai.code_execution.sandbox import landlock, seccomp

    monkeypatch.setattr(landlock, "abi_version", lambda: abi)
    monkeypatch.setattr(seccomp, "unsupported_reason", lambda: seccomp_reason)


@pytest.mark.parametrize("abi", [0, 6])
def test_startup_reports_the_in_process_refusal_with_its_own_remedy(monkeypatch, abi):
    _environment(monkeypatch, "production", "inprocess")
    _kernel(monkeypatch, abi)
    problem, remedy = runner.startup_sandbox_problem()
    assert problem and problem == config.inprocess_refusal_reason()
    assert remedy == runner.INPROCESS_REMEDY


@pytest.mark.parametrize("abi,seccomp_reason", [(3, ""), (6, "unsupported seccomp architecture: riscv64")])
def test_startup_reports_missing_confinement_with_the_kernel_remedy(monkeypatch, abi, seccomp_reason):
    _environment(monkeypatch, "production", "subprocess")
    _kernel(monkeypatch, abi, seccomp_reason)
    problem, remedy = runner.startup_sandbox_problem()
    assert problem
    assert remedy == CONFINEMENT_REMEDY


@pytest.mark.parametrize("environment,mode,abi", [
    ("production", "subprocess", 6),
    ("development", "inprocess", 0),
    ("development", "subprocess", 0),
])
def test_startup_reports_nothing_when_code_can_run(monkeypatch, environment, mode, abi):
    _environment(monkeypatch, environment, mode)
    _kernel(monkeypatch, abi)
    assert runner.startup_sandbox_problem() == ("", "")
