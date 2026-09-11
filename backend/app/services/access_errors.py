"""Recognising "this identity may not read that data" in a failed query run.

A reader who can use a data source may still be refused ONE dataset inside it
— the standard Power BI setup, where item-level sharing and RLS decide per
semantic model. That refusal only surfaces when the query runs, as the
provider's HTTP error bubbled up through the client
(`DAX query failed: HTTP 401 {...response body...}`), so it reached the viewer
verbatim: a status code instead of an explanation, plus a response body that
can quote model and table names.

Access at the data-source level is classified elsewhere, when the reader's
client cannot be built at all (report_service's `data_source_errors`, codes
`credentials_required` / `no_access`). This covers the per-dataset case those
never see, so both land on the same "no access" state.
"""
from __future__ import annotations

import re

NO_ACCESS_CODE = "no_access"

# The one sentence a refused reader is shown. Deliberately fixed: the
# provider's own text is never passed through, because it can name the very
# models the reader was refused.
NO_ACCESS_REASON = "You do not have access to the data behind this query."

# Every client reports provider failures as "<operation> failed: HTTP <status>
# <body>". 401/403 at query time mean the identity was refused — its token was
# already resolved and refreshed when the client was built, so an expired
# credential fails earlier, as credentials_required. Power BI answers a model
# the identity cannot see with 404 PowerBIEntityNotFound rather than 403;
# a bare 404 is left alone, since elsewhere it can simply mean a wrong id.
#
# Known conflation, accepted deliberately: PowerBIEntityNotFound is also what a
# genuinely DELETED model returns, so a stale reference is reported as "no
# access" and, during fork hydration, counted as a refusal. The alternative is
# to distinguish them by asking the provider whether the model exists — which
# is the disclosure ("this model exists, you just cannot see it") that the
# fixed reason and the withheld response body exist to prevent. Reporting a
# broken reference as a refusal is the safe direction of the two; the real
# cause is in the logs (hydrate_fork logs every failure with its exception).
_ACCESS_DENIED = re.compile(r"\bHTTP\s+(?:401|403)\b|PowerBIEntityNotFound", re.IGNORECASE)


def is_access_denied(error: object) -> bool:
    """True when a failed run's error is the provider refusing this identity."""
    return bool(error) and bool(_ACCESS_DENIED.search(str(error)))
