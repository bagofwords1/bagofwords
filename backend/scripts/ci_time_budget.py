#!/usr/bin/env python3
"""Report the slowest test setups from JUnit XML, and warn on tests over budget.

Report-only: this always exits 0, even on a missing or broken file, so it can
never fail a run. CI uses it to keep slow setups visible.

Setup times come from the `setup_seconds` property written by
tests/ci_phase_times.py. Without that property (older XML), the slowest list
falls back to total time and says so.

To make it blocking later:
  1. Choose budgets the suite already meets. The defaults are not that yet: the
     dashboard corpus setup takes 20-34 s and some tests exceed 120 s in total.
  2. In main(), return 1 when any test is over budget.
  3. Remove `continue-on-error: true` from the "Report slow tests" step in
     .github/workflows/e2e-tests.yml.

Usage: ci_time_budget.py [--top N] [--setup-budget S] [--total-budget S] FILE...
Stdlib only, so it runs without the backend environment.
"""
import argparse
import sys
import xml.etree.ElementTree as ET


def read_tests(path):
    """Return (name, total_seconds, setup_seconds or None) for each testcase."""
    tests = []
    for case in ET.parse(path).getroot().iter("testcase"):
        setup = None
        for prop in case.iter("property"):
            if prop.get("name") == "setup_seconds":
                setup = float(prop.get("value"))
        total = float(case.get("time") or 0)
        tests.append((f"{case.get('classname')}::{case.get('name')}", total, setup))
    return tests


def report(path, tests, args):
    total = sum(t[1] for t in tests)
    timed = [t for t in tests if t[2] is not None]
    print(f"{path}: {len(tests)} tests, {total / 3600:.2f} worker-hours")

    if timed:
        setup = sum(t[2] for t in timed)
        print(f"Setup is {setup / total:.0%} of test time ({setup / 3600:.2f} worker-hours)" if total else "No test time recorded")
        slowest = sorted(timed, key=lambda t: t[2], reverse=True)
        print(f"Slowest {args.top} setups (seconds):")
        for name, _, seconds in slowest[: args.top]:
            print(f"  {seconds:8.1f}  {name}")
    else:
        print(f"No setup_seconds in this file. Slowest {args.top} tests by total time (seconds):")
        for name, seconds, _ in sorted(tests, key=lambda t: t[1], reverse=True)[: args.top]:
            print(f"  {seconds:8.1f}  {name}")

    over = [t for t in tests if t[1] > args.total_budget or (t[2] or 0) > args.setup_budget]
    if over:
        over.sort(key=lambda t: t[1], reverse=True)
        print(f"{len(over)} tests over budget (setup > {args.setup_budget:g} s or total > {args.total_budget:g} s):")
        for name, seconds, setup_seconds in over:
            setup_text = "" if setup_seconds is None else f", setup {setup_seconds:.1f} s"
            print(f"  {seconds:8.1f}  {name}{setup_text}")
        # One annotation per file: GitHub shows only the first 10 per step.
        print(f"::warning::{len(over)} tests over the time budget in {path}. Slowest: {over[0][0]} ({over[0][1]:.0f} s)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report slow test setups from JUnit XML. Always exits 0.")
    parser.add_argument("files", nargs="+")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--setup-budget", type=float, default=45.0, help="warn above this many seconds of setup")
    parser.add_argument("--total-budget", type=float, default=120.0, help="warn above this many seconds in total")
    args = parser.parse_args(argv)

    for path in args.files:
        try:
            report(path, read_tests(path), args)
        except (OSError, ValueError, TypeError, ET.ParseError) as error:
            # A killed run leaves no XML. That is a fact to report, not a failure.
            print(f"{path}: no timing report ({error})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
