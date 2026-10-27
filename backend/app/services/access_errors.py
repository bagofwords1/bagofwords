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

from fastapi import HTTPException

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
# Anchored to the "<operation> failed: HTTP <status>" shape every client
# raises (powerbi, powerbi_report_server, tableau, sisense, xmla_base, ...)
# rather than matching a bare status anywhere in the string: the body is
# interpolated right after it, so an unanchored pattern also fired on a 403
# quoted INSIDE a provider response — relabelling an unrelated failure "no
# access", dropping its real cause from the step, and counting it as a
# refusal during fork hydration.
_ACCESS_DENIED = re.compile(
    r"failed:\s*HTTP\s+(?:401|403)\b|PowerBIEntityNotFound", re.IGNORECASE
)


def is_access_denied(error: object) -> bool:
    """True when a failed run's error is the provider refusing this identity."""
    return bool(error) and bool(_ACCESS_DENIED.search(str(error)))


# ── a reader who must sign in ────────────────────────────────────────────
#
# A connection each person signs in to (auth_policy user_required) cannot be
# built for a reader without an active sign-in of their own. That reader has
# access and needs to connect -- the viewer gate's "connect your account"
# state -- which is neither a broken connection nor a refusal. The cause is
# typed where it is raised (connection_service / data_source_service), so it
# is recognised by type, not by its wording; a chart that ran without the
# client stores this fixed reason, as a refused one stores NO_ACCESS_REASON.

CREDENTIALS_REQUIRED_CODE = "credentials_required"
CREDENTIALS_REQUIRED_REASON = "Connect your account to see the data behind this query."


class CredentialsRequiredError(HTTPException):
    """The reader must sign in to this connection: a 403, so every caller
    that already handles an HTTPException is unchanged."""

    def __init__(self, detail: str = "User credentials required"):
        super().__init__(status_code=403, detail=detail)


# The two sentences CredentialsRequiredError is raised with. A chart's code
# runs in the sandbox, so what comes back from it is the message, not the
# exception: these are recognised there, and only there.
_CREDENTIALS_REQUIRED_TEXT = re.compile(r"\b(?:connect|user credentials) required\b", re.IGNORECASE)


def needs_credentials(error: object) -> bool:
    """True when a failure is a reader's missing sign-in: the typed cause,
    a stand-in client's error that wraps it (ConnectionUnavailableError keeps
    its cause), or -- from a chart's sandboxed run -- its message."""
    seen, current = 0, error
    while current is not None and seen < 5:
        if isinstance(current, CredentialsRequiredError):
            return True
        current = getattr(current, "cause", None)
        seen += 1
    return bool(error) and not isinstance(error, HTTPException) and bool(
        _CREDENTIALS_REQUIRED_TEXT.search(str(error)))


def source_error_code(error: object) -> str:
    """The machine-readable cause of a data source that could not be built
    for a reader: credentials_required, no_access or connection_failed."""
    if needs_credentials(error):
        return CREDENTIALS_REQUIRED_CODE
    detail = str(getattr(error, "detail", None) or error).lower()
    if "credentials required" in detail:
        return CREDENTIALS_REQUIRED_CODE
    if "do not have access" in detail:
        return NO_ACCESS_CODE
    return "connection_failed"


def viewer_error_code(status: str | None, reason: str | None) -> str | None:
    """The code a reader's stored failed run carries, from its fixed reason."""
    if status != "error":
        return None
    if reason == NO_ACCESS_REASON:
        return NO_ACCESS_CODE
    if reason == CREDENTIALS_REQUIRED_REASON:
        return CREDENTIALS_REQUIRED_CODE
    return None
