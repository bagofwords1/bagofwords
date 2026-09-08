"""Shared constants for the diagnosis explorer."""
from datetime import timedelta

# Bump when the rollup logic changes: the startup sweep re-indexes every run
# whose ``rollup_version`` is older, so a fix to how cost or a score is
# computed reaches historical rows without a hand-run script.
ROLLUP_VERSION = 1

# A run still ``in_progress`` this long after it started never finished (the
# process died mid-run): the query language calls it ``status:stale``, the
# table labels it, and the sweep indexes it instead of waiting for a finish
# hook that will not come.
STALE_AFTER = timedelta(hours=1)
