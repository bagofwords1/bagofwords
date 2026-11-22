"""The limits one organization's custom tables are held to.

The defaults are the constants this feature shipped with, so an organization
that sets nothing behaves exactly as before. An organization can move them
through its settings, inside a range enforced here: a setting can raise or
lower a limit but cannot remove it.

The ranges, and why:

* **rows** 1,000 to 100,000,000. The artifact is a local DuckDB file, which
  handles tens of millions of rows; extraction is what costs. Past 100M the
  per-session filtered catalog built for row-level security (fast/rls.py) and
  the size of the file on the volume both stop being reasonable defaults to
  opt into from a settings page.
* **size** 1 to 64 GiB. Guards the estimated result and the artifact on disk;
  the volume is shared by every table of every connection.
* **source scan** 1 to 10,240 GiB (10 TiB). What a consumption-priced
  warehouse may read per refresh. The scan repeats on every refresh, so the
  ceiling is high enough for a large rollup and low enough that one setting
  cannot authorize an unbounded bill.
* **requests** 16 to 10,000 per extraction, for sources read over a
  request-per-page API. Power BI allows about 120 requests a minute, so the
  ceiling is roughly an hour and a half of one identity's rate budget.

Two layers apply the range: `validate_setting` refuses an out-of-range write,
and `resolve` clamps whatever is stored, so a value written before a range
changed (or straight into the database) still cannot lift a limit.
"""

from dataclasses import dataclass
from typing import NamedTuple

GIB = 1024 * 1024 * 1024


class Bound(NamedTuple):
    default: int
    low: int
    high: int


# Setting key -> the range it is held to. Sizes are stored in whole GiB.
SETTINGS: dict = {
    "custom_table_max_rows": Bound(5_000_000, 1_000, 100_000_000),
    "custom_table_max_gb": Bound(2, 1, 64),
    "custom_table_max_scan_gb": Bound(100, 1, 10_240),
    "custom_table_max_requests": Bound(512, 16, 10_000),
}


@dataclass(frozen=True)
class FastLimits:
    """What one extraction may read and store."""

    max_rows: int = SETTINGS["custom_table_max_rows"].default
    max_bytes: int = SETTINGS["custom_table_max_gb"].default * GIB
    max_scan_bytes: int = SETTINGS["custom_table_max_scan_gb"].default * GIB
    # Requests one extraction may make to a source read page by page.
    max_requests: int = SETTINGS["custom_table_max_requests"].default


DEFAULTS = FastLimits()


def validate_setting(key: str, value) -> int:
    """`value` as this setting's integer, or ValueError naming the range."""
    bound = SETTINGS[key]
    if isinstance(value, bool) or not isinstance(value, int) \
            or not bound.low <= value <= bound.high:
        raise ValueError(
            f"{key} must be a whole number between {bound.low:,} and {bound.high:,}."
        )
    return value


def _stored(config, key: str) -> int:
    """The stored value clamped into its range; the default when unusable."""
    bound = SETTINGS[key]
    raw = (config or {}).get(key)
    if isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return bound.default
    return max(bound.low, min(bound.high, int(raw)))


def resolve(organization_settings) -> FastLimits:
    """The limits for an organization, from its settings row (or None)."""
    config = getattr(organization_settings, "config", None)
    if not isinstance(config, dict):
        return DEFAULTS
    return FastLimits(
        max_rows=_stored(config, "custom_table_max_rows"),
        max_bytes=_stored(config, "custom_table_max_gb") * GIB,
        max_scan_bytes=_stored(config, "custom_table_max_scan_gb") * GIB,
        max_requests=_stored(config, "custom_table_max_requests"),
    )


def of(client) -> FastLimits:
    """The limits attached to a client being extracted from.

    Sources are built from the client alone (`sources.source_for`), so the
    service attaches the organization's limits to the client it constructs and
    everything downstream reads them here. A client nobody attached limits to
    (a unit test, a script) gets the defaults.
    """
    limits = getattr(client, "extraction_limits", None)
    return limits if isinstance(limits, FastLimits) else DEFAULTS
