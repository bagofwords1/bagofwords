"""Per-user copies of a custom table: what one is built from, and when it may be served.

A custom table on a `system_only` connection is one shared artifact. On a
`user_required` connection that cannot work: the artifact would hold the rows
of whoever built it and hand them to every reader. There, each user gets an
artifact of their own, extracted with their own credentials, so the source's
own row-level security (a Postgres policy, a Power BI RLS role, a warehouse
grant) decides what is in it. BOW adds no rule of its own and holds no
mapping to drift from.

This module is the part with no I/O: the two fingerprints a copy is tied to,
and the freshness decision. Storage, building and cleanup live in
`app/services/custom_table_user_copy_service.py`.

**A copy is tied to what it was built from.**

* `definition_hash` — the query (and Power BI target). Edit the query and
  every copy of it is unusable from that moment, not from its next refresh.
* `credential_fingerprint` — *which login* the rows were read with. A user who
  replaces their stored database login, or reconnects with another Microsoft
  account, must not be served rows read as the previous one.

**Freshness has a soft and a hard edge.**

* Past the table's own refresh schedule a copy is *stale*: still that user's
  own rows, so it is served, and rebuilt in the background.
* Past the organization's maximum age — or invalidated, or built from
  something else — it is *expired* and is not served at all. That maximum is
  what bounds how long a change to someone's access at the source can go
  unnoticed: the copy is a snapshot of what they could see when it was built.
"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Any, Optional

# Why a copy cannot be served (State.reason), in the order they are checked.
REASON_MISSING = "missing"                 # never built, or its file is gone
REASON_DEFINITION = "definition_changed"
REASON_CREDENTIALS = "credentials_changed"
REASON_INVALIDATED = "invalidated"
REASON_EXPIRED = "expired"
REASON_STALE = "stale"                     # served, and rebuilt behind it

# Credential fields that change without the identity changing: a refreshed
# access token is the same person.
_VOLATILE = {
    "access_token", "refresh_token", "id_token", "expires_at", "expires_in",
    "ext_expires_in", "token_type", "scope", "obtained_at",
}
# Claims that name the signed-in account inside a delegated access token.
_IDENTITY_CLAIMS = ("tid", "oid", "sub", "upn", "preferred_username", "unique_name", "appid")


def _jwt_claims(token: Any) -> dict:
    """The payload of a JWT, unverified. Only ever used to tell two accounts
    apart, never to trust one."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body.encode()))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def credential_fingerprint(credentials: Optional[dict], secret: str) -> str:
    """A stable, keyed digest of *who* a set of resolved credentials is.

    Keyed with the deployment's secret so the stored value cannot be used to
    test a guessed password. Token material is left out and replaced by the
    account the token names, so refreshing a token does not look like a
    change of identity while signing in as someone else does.
    """
    creds = credentials if isinstance(credentials, dict) else {}
    stable = {k: v for k, v in creds.items() if k not in _VOLATILE}
    claims = _jwt_claims(creds.get("access_token"))
    account = {k: claims[k] for k in _IDENTITY_CLAIMS if claims.get(k)}
    if account:
        stable["__account__"] = account
    elif creds.get("access_token"):
        # An opaque token: nothing in it names the account, so the token is
        # the only identity there is. Every refresh rebuilds the copy, which
        # is slow but never wrong.
        stable["__token__"] = hashlib.sha256(str(creds["access_token"]).encode()).hexdigest()
    payload = json.dumps(stable, sort_keys=True, default=str, separators=(",", ":"))
    return hmac.new((secret or "").encode(), payload.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class State:
    """Whether a copy may be served, and whether it should be rebuilt."""

    servable: bool
    rebuild: bool
    reason: str = ""

    @property
    def fresh(self) -> bool:
        return self.servable and not self.rebuild


FRESH = State(servable=True, rebuild=False)


def schedule_due_at(now: datetime, mode: Optional[str], interval_minutes: Optional[int],
                    at_time: Optional[str], tz=None) -> Optional[datetime]:
    """The moment (naive UTC) before which a copy counts as stale under the
    table's own schedule, or None when the table has none.

    `interval`: anything older than the interval. `time`: anything older than
    the most recent occurrence of that time of day in the organization's zone.
    """
    if mode == "time" and at_time:
        try:
            hh, mm = (int(x) for x in str(at_time).split(":")[:2])
        except ValueError:
            return None
        if tz is None:
            local_now = now
            due = datetime.combine(local_now.date(), dtime(hh, mm))
            if due > local_now:
                due -= timedelta(days=1)
            return due
        from datetime import timezone as _tz

        local_now = now.replace(tzinfo=_tz.utc).astimezone(tz)
        due = datetime.combine(local_now.date(), dtime(hh, mm), tzinfo=tz)
        if due > local_now:
            due -= timedelta(days=1)
        return due.astimezone(_tz.utc).replace(tzinfo=None)
    if interval_minutes and interval_minutes > 0:
        return now - timedelta(minutes=int(interval_minutes))
    return None


def evaluate(
    *,
    status: Optional[str],
    has_artifact: bool,
    refreshed_at: Optional[datetime],
    invalidated_at: Optional[datetime],
    copy_definition_hash: Optional[str],
    definition_hash: str,
    copy_fingerprint: Optional[str],
    fingerprint: Optional[str],
    now: datetime,
    max_age_seconds: int,
    stale_before: Optional[datetime] = None,
    refresh_requested_at: Optional[datetime] = None,
) -> State:
    """Decide what to do with one copy.

    `fingerprint=None` means the caller cannot resolve the user's credentials
    here (the schema context, which only needs to know what the client will
    hold): the credential check is skipped, and the caller relies on a
    mismatch having been recorded as an invalidation when it was found.
    """
    if not has_artifact or refreshed_at is None or status not in ("ok", "building", "error"):
        return State(False, True, REASON_MISSING)
    if copy_definition_hash != definition_hash:
        return State(False, True, REASON_DEFINITION)
    if fingerprint is not None and copy_fingerprint != fingerprint:
        return State(False, True, REASON_CREDENTIALS)
    if invalidated_at is not None and invalidated_at >= refreshed_at:
        return State(False, True, REASON_INVALIDATED)
    if (now - refreshed_at).total_seconds() > max_age_seconds:
        return State(False, True, REASON_EXPIRED)
    if stale_before is not None and refreshed_at < stale_before:
        return State(True, True, REASON_STALE)
    if refresh_requested_at is not None and refresh_requested_at >= refreshed_at:
        return State(True, True, REASON_STALE)
    return FRESH


def is_per_user_connection(connection) -> bool:
    """Whether custom tables on `connection` are cached once per user."""
    return (getattr(connection, "auth_policy", None) or "system_only") == "user_required"
