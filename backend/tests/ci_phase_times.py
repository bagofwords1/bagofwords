"""Record each test's setup time in the JUnit XML. Reporting only.

JUnit's `time` attribute is setup + call + teardown, which hides the number CI
most needs: what a test costs before it starts. Load with
`-p tests.ci_phase_times`; backend/scripts/ci_time_budget.py reads the result.
"""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "setup":
        item.user_properties.append(("setup_seconds", f"{report.duration:.3f}"))
