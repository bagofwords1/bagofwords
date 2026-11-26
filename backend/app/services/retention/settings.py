"""The three retention settings and how to read them.

Each is a number of days, stored as a FeatureConfig in the org's settings.
Off is stored as ``0`` (FeatureConfig turns a ``None`` value into a bool, so
"empty" cannot be stored as null); the settings page shows ``0`` as an
empty field. The API takes empty (``None``, ``""``) or ``0`` for off, and
``1..MAX_RETENTION_DAYS`` to delete, every night, what has been untouched for
that long. Retention deletes for good: nothing is archived or kept to restore.
"""
from __future__ import annotations

from typing import Any, Optional

REPORT_RETENTION_KEY = "report_retention_days"
ARTIFACT_RETENTION_KEY = "artifact_retention_days"
FILE_RETENTION_KEY = "file_retention_days"
RETENTION_KEYS = (REPORT_RETENTION_KEY, ARTIFACT_RETENTION_KEY, FILE_RETENTION_KEY)

MAX_RETENTION_DAYS = 3650


def normalize_setting(value: Any) -> int:
    """The value to store for an update, or ``ValueError``.

    ``None`` / ``""`` -> ``0`` (off); an int 0..MAX -> that int. Anything
    else (strings of digits, bools, floats, negatives) is refused rather than
    coerced, so the admin sees that what they typed is not what is in force.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_RETENTION_DAYS:
        raise ValueError(
            f"Retention must be empty or a whole number of days between 0 and {MAX_RETENTION_DAYS} "
            "(empty or 0 keeps everything)."
        )
    return value


def days_from_config(config: Optional[dict], key: str) -> int:
    """The org's retention for ``key`` in days, or 0 (off) when unset/invalid."""
    raw = (config or {}).get(key)
    value = raw.get("value") if isinstance(raw, dict) else raw
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return min(value, MAX_RETENTION_DAYS)
