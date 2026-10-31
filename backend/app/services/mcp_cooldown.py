"""Bounded per-principal connector cooldowns; never automatically replay a write.

A worker suppresses subsequent calls after a provider asks it to back off.
Already-in-flight calls and other workers still rely on the provider's limits.
"""
import math
import re
import time

_deadlines: dict[tuple[str, str, str], float] = {}


def retry_after(error: str) -> int | None:
    value = str(error or '')
    if not re.search(r'rate.?limit|too many requests|\b429\b', value, re.I):
        return None
    match = re.search(r'(?:retry[\s_-]*after|try again in)[:\s]*(\d+(?:\.\d+)?)(?:\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h))?', value, re.I)
    if not match:
        return 60
    unit = (match[2] or 's').lower()
    scale = 3600 if unit.startswith('h') else 60 if unit.startswith('m') else 1
    return min(86400, max(1, math.ceil(float(match[1]) * scale)))


def remaining(key: tuple[str, str, str]) -> int:
    return max(0, math.ceil(_deadlines.get(key, 0) - time.monotonic()))


def record(key: tuple[str, str, str], error: str) -> int | None:
    seconds = retry_after(error)
    if seconds is None:
        return None
    now = time.monotonic()
    for old in list(_deadlines):
        if _deadlines[old] <= now:
            del _deadlines[old]
    if len(_deadlines) >= 4096 and key not in _deadlines:
        del _deadlines[min(_deadlines, key=_deadlines.get)]
    _deadlines[key] = max(_deadlines.get(key, 0), now + seconds)
    return seconds
