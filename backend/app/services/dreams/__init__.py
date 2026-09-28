"""Overnight learning: the nightly agent dream and user dream.

- ``common``      settings, night window, constants (pure helpers)
- ``runtime``     the hourly sweep, exactly-once claims, budget, run log
- ``agent_dream`` per-agent instruction consolidation → one suggestion build
- ``user_dream``  per-user reflection → memory, open threads, check-ins, habits
- ``briefing``    the session-start "Since you were here" items

See docs/design/overnight-learning.md.
"""
