"""Shared 'current time' rendering for planner prompts.

Renders the per-turn timestamp in the organization's configured timezone when
one is set (Settings → General), falling back to the server's local timezone —
the prior behaviour — when it is unset or invalid. Storage is unaffected; this
only changes the clock the model is told about.

Beyond the wall-clock time this module also spells out, in plain text, the two
things LLMs get wrong when left to compute them from a bare ``YYYY-MM-DD``:

* the **weekday** of "today" (models are unreliable at date→weekday arithmetic), and
* the **work-week convention** — which day the week starts on and the concrete
  date range of "this week". This matters for locales whose business week does
  not start on Monday (e.g. Hebrew/Arabic orgs run Sunday–Thursday), where
  "this week"/"last week" would otherwise be interpreted against the wrong
  boundary.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Full weekday names indexed by Python's ``date.weekday()`` (Monday == 0).
_WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]

# ``date.weekday()`` value for each supported first-day-of-week setting.
_WEEK_START_TO_WEEKDAY = {"monday": 0, "saturday": 5, "sunday": 6}

# Locales whose business week conventionally starts on Sunday. Used only when
# the org's week_start setting is unset/"auto" to pick a sensible default; an
# explicit org setting always wins over this. The language subtag is matched,
# so "he", "he-IL", "ar", "ar-EG" all resolve to Sunday.
_SUNDAY_FIRST_LOCALES = {"he", "ar", "fa", "ur"}


def now_in_org_tz(timezone: Optional[str]) -> Tuple[datetime, str]:
    """Return (now, tz_label) for the org timezone, or local time as fallback."""
    if timezone:
        try:
            now = datetime.now(ZoneInfo(timezone))
            return now, timezone
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    now = datetime.now().astimezone()
    return now, str(now.tzinfo)


def resolve_first_weekday(week_start: Optional[str], locale: Optional[str]) -> int:
    """Return the ``date.weekday()`` value of the org's first day of the week.

    ``week_start`` is the explicit org setting ("monday"/"sunday"/"saturday",
    or None/"auto" to derive from ``locale``). An explicit, recognised value
    always wins; otherwise the locale's language subtag decides (Sunday for
    he/ar/fa/ur, Monday — the ISO default — for everyone else).
    """
    if week_start:
        weekday = _WEEK_START_TO_WEEKDAY.get(week_start.strip().lower())
        if weekday is not None:
            return weekday
    if locale:
        language = locale.strip().lower().replace("_", "-").split("-")[0]
        if language in _SUNDAY_FIRST_LOCALES:
            return _WEEK_START_TO_WEEKDAY["sunday"]
    return _WEEK_START_TO_WEEKDAY["monday"]


def _week_range(today: date, first_weekday: int) -> Tuple[date, date]:
    """Return (start, end) dates of the week containing ``today``.

    The week begins on ``first_weekday`` (a ``date.weekday()`` value) and spans
    seven days, so ``end`` is the day before the next week's first day.
    """
    offset = (today.weekday() - first_weekday) % 7
    start = today - timedelta(days=offset)
    return start, start + timedelta(days=6)


def _week_convention_str(today: date, first_weekday: int) -> str:
    """'week starts on <Day>; this week: <start> (<Abbr>) to <end> (<Abbr>)'."""
    start, end = _week_range(today, first_weekday)
    start_abbr = _WEEKDAYS[start.weekday()][:3]
    end_abbr = _WEEKDAYS[end.weekday()][:3]
    return (
        f"week starts on {_WEEKDAYS[first_weekday]}; "
        f"this week: {start.isoformat()} ({start_abbr}) to {end.isoformat()} ({end_abbr})"
    )


def current_time_str(
    timezone: Optional[str],
    week_start: Optional[str] = None,
    locale: Optional[str] = None,
) -> str:
    """'YYYY-MM-DD HH:MM:SS, <Weekday>; timezone: <tz>; <week convention>'."""
    now, tz = now_in_org_tz(timezone)
    weekday = _WEEKDAYS[now.weekday()]
    first_weekday = resolve_first_weekday(week_start, locale)
    convention = _week_convention_str(now.date(), first_weekday)
    return (
        f"{now.strftime('%Y-%m-%d %H:%M:%S')}, {weekday}; "
        f"timezone: {tz}; {convention}"
    )


def time_block(
    timezone: Optional[str],
    week_start: Optional[str] = None,
    locale: Optional[str] = None,
) -> str:
    """Render the per-turn ``<time>`` block for the planner user message.

    Includes the weekday of "today" and the org's week convention so the model
    never has to compute either from a bare date.
    """
    now, tz = now_in_org_tz(timezone)
    weekday = _WEEKDAYS[now.weekday()]
    first_weekday = resolve_first_weekday(week_start, locale)
    convention = _week_convention_str(now.date(), first_weekday)
    return (
        "<time>\n"
        f"  now: {now.strftime('%Y-%m-%d %H:%M:%S')} ({tz}), {weekday}\n"
        f"  {convention}\n"
        "</time>"
    )
