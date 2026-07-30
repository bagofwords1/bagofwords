"""Row-level security on accelerated relations.

A custom query is materialized once with a shared credential, so every agent
that activates it sees the same rows. RLS makes that copy safe to expose to
people who should each see a slice of it, by filtering at read time against who
is asking.

**Enforcement is the catalog, not a rewrite.** Agent-generated SQL is
arbitrary — subqueries, CTEs, unions and lateral joins give a dozen ways to
lose an appended predicate. Instead the per-session DuckDB catalog holds only
the filtered rows, under the relation's own name, and the raw artifact is
DETACHed before any agent SQL runs. A filtered view over an attached artifact
was not enough: the alias stays nameable (and discoverable through
`duckdb_databases()`), so `SELECT * FROM bow_a0.sales` read straight past it.

**Values are bound, never interpolated.** The allowed values go into a
session-local table via a parameterised INSERT and the filter semi-joins
against it, so a user's own `department` string never becomes SQL text. The
column name *is* interpolated (nothing else can name a column), so it is
checked against the relation's materialized column list first and rejected if
absent — a policy naming a column that does not exist denies rather than
errors.

**Unresolved means denied.** If the identity carries no value for the attribute
a policy binds to — a user who has never logged in through the IdP, a
background job with no user at all — the default is zero rows. `rls_default_deny`
can invert that per relation, but it starts closed, because the failure mode of
the opposite default is handing over the whole table.

Phase 1 is attribute mode only: a column, an identity source, and per-principal
grants. SQL mode, the conformance check against the source's own answer, and
per-group materialization are phase 2+ (docs/design/custom-query-rls.md).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)

MODE_ATTRIBUTE = "attribute"
MODE_SQL = "sql"          # phase 2 — accepted by the model, not yet compiled

# Identity sources a policy may bind to. Anything else is a configuration error
# and denies, rather than silently matching nothing.
SOURCE_PROFILE_PREFIX = "profile."
SOURCE_EMAIL = "user.email"
SOURCE_GROUPS = "groups"
SOURCE_ROLES = "roles"

# Operators. `in` is the only one phase 1 needs: equality is `in` with one
# value, and anything richer (ranges, LIKE) belongs in SQL mode where the admin
# is already writing an expression.
OP_IN = "in"

WILDCARD = "*"

_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]{0,127}$")


@dataclass
class Identity:
    """Everything a policy may bind to, resolved once per request.

    Deliberately a plain value object with no DB access: the compiler must be
    testable without a session, and an identity that is expensive to build
    would tempt callers into caching it across users.
    """

    user_id: Optional[str] = None
    email: Optional[str] = None
    profile_attributes: Dict[str, Any] = field(default_factory=dict)
    group_ids: Set[str] = field(default_factory=set)
    group_names: Set[str] = field(default_factory=set)
    role_ids: Set[str] = field(default_factory=set)
    role_names: Set[str] = field(default_factory=set)
    # Reserved for a future trusted internal context. Nothing sets it in phase
    # 1: there is no legitimate RLS bypass yet, and a flag that only ever reads
    # False is safer than one that a caller can discover and set.
    is_system: bool = False

    @property
    def is_anonymous(self) -> bool:
        return self.user_id is None and not self.is_system


# A background path with no user is NOT a bypass. Scheduled refreshes extract
# with the connection's own credential and never read through RLS; anything
# that serves rows must name a real person.
ANONYMOUS = Identity()


@dataclass
class Filter:
    """The compiled result of a policy against one identity."""

    # None → no restriction; the artifact is served attached, without a copy.
    allowed_values: Optional[List[str]] = None
    # True → zero rows, whatever the values say.
    deny_all: bool = False
    column: Optional[str] = None
    reason: str = ""

    @property
    def unrestricted(self) -> bool:
        return not self.deny_all and self.allowed_values is None


UNRESTRICTED = Filter(reason="no policy")


def _as_values(raw: Any) -> List[str]:
    """Normalise an identity value to a list of strings.

    Profile attributes arrive from Graph as scalars, but a multi-valued
    attribute (or a groups/roles source) is a list. Both are legitimate.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(v) for v in raw if v is not None and str(v) != ""]
    s = str(raw)
    return [s] if s else []


def resolve_source(source: str, identity: Identity) -> List[str]:
    """The identity's own values for `source`.

    An unknown source returns nothing, which combined with default-deny means a
    typo in a policy closes the relation rather than opening it.
    """
    if not source:
        return []
    if source == SOURCE_EMAIL:
        return _as_values(identity.email)
    if source == SOURCE_GROUPS:
        return sorted(identity.group_names)
    if source == SOURCE_ROLES:
        return sorted(identity.role_names)
    if source.startswith(SOURCE_PROFILE_PREFIX):
        key = source[len(SOURCE_PROFILE_PREFIX):]
        return _as_values((identity.profile_attributes or {}).get(key))
    logger.warning("rls.unknown_source", extra={"source": source})
    return []


def _principal_matches(grant: Dict[str, Any], identity: Identity) -> bool:
    """Does this grant apply to this identity?

    Groups and roles are matched by id *or* name. Id is what the UI writes and
    is stable across renames; name is what an admin editing JSON by hand will
    reach for, and rejecting it would be a papercut with no security benefit —
    both are org-scoped and already resolved from the same tables.
    """
    ptype = (grant.get("principal_type") or "").lower()
    pid = str(grant.get("principal_id") or "")
    if not pid:
        return False
    if ptype == "user":
        return pid == str(identity.user_id or "")
    if ptype == "group":
        return pid in identity.group_ids or pid in identity.group_names
    if ptype == "role":
        return pid in identity.role_ids or pid in identity.role_names
    return False


def compile_policy(
    policy: Optional[Dict[str, Any]],
    identity: Identity,
    *,
    available_columns: Sequence[str] = (),
    default_deny: bool = True,
    mode: str = MODE_ATTRIBUTE,
) -> Filter:
    """Turn a stored policy into the filter to apply for `identity`.

    Never raises. Every way a policy can be wrong — unknown mode, missing
    column, unknown source, no matching grant — resolves to deny (or to the
    relation's `default_deny` where the design says the admin gets to choose),
    because the alternative is returning rows nobody authorised.
    """
    if not policy:
        return Filter(deny_all=default_deny, reason="rls enabled with no policy")

    if mode == MODE_SQL:
        # Phase 2. Accepting the row but not the semantics would serve
        # unfiltered data under a policy the admin believes is enforced.
        return Filter(deny_all=True, reason="sql mode is not implemented yet")
    if mode != MODE_ATTRIBUTE:
        return Filter(deny_all=True, reason=f"unknown rls mode '{mode}'")

    column = str(policy.get("column") or "").strip()
    if not column:
        return Filter(deny_all=True, reason="policy names no column")
    if not _COLUMN_RE.match(column):
        return Filter(deny_all=True, reason="policy column name is not a plain identifier")
    if available_columns and column not in set(available_columns):
        # The materialized relation changed shape under the policy. Denying is
        # the only safe reading: we cannot filter on a column that is gone.
        return Filter(
            deny_all=True,
            reason=f"policy column '{column}' is not in the materialized relation",
        )

    op = (policy.get("op") or OP_IN).lower()
    if op != OP_IN:
        return Filter(deny_all=True, reason=f"unsupported operator '{op}'")

    # Grants first: a wildcard grant means this principal sees everything and
    # the attribute never has to resolve.
    granted: List[str] = []
    matched_any = False
    for grant in policy.get("grants") or []:
        if not isinstance(grant, dict) or not _principal_matches(grant, identity):
            continue
        matched_any = True
        values = _as_values(grant.get("values"))
        if WILDCARD in values:
            return Filter(column=column, reason="wildcard grant")
        granted.extend(values)

    own = resolve_source(str(policy.get("source") or ""), identity)
    allowed = sorted({*own, *granted})

    if not allowed:
        if default_deny:
            return Filter(
                deny_all=True,
                column=column,
                reason=(
                    "no grant matched and the identity has no value for "
                    f"'{policy.get('source')}'"
                ),
            )
        return Filter(column=column, reason="unresolved attribute, default-deny is off")

    return Filter(
        column=column,
        allowed_values=allowed,
        reason="granted" if matched_any else "own attribute value",
    )


def validate_policy(
    policy: Optional[Dict[str, Any]],
    mode: str,
    available_columns: Sequence[str] = (),
) -> None:
    """Reject a policy at save time. Raises ValueError with an admin-readable
    message.

    Save-time validation is not a substitute for compile-time denial — the
    relation's columns can change afterwards — but an admin should learn about
    a typo when they press Save, not by wondering why a report is empty.
    """
    if mode == MODE_SQL:
        raise ValueError("SQL mode is not available yet; use attribute mode.")
    if mode != MODE_ATTRIBUTE:
        raise ValueError(f"Unknown RLS mode '{mode}'.")
    if not isinstance(policy, dict) or not policy:
        raise ValueError("An RLS policy is required when RLS is enabled.")

    column = str(policy.get("column") or "").strip()
    if not column:
        raise ValueError("Choose the column the policy filters on.")
    if not _COLUMN_RE.match(column):
        raise ValueError(f"'{column}' is not a valid column name.")
    if available_columns and column not in set(available_columns):
        raise ValueError(
            f"'{column}' is not a column of this query's result. "
            f"Available: {', '.join(sorted(available_columns)[:20])}"
        )

    source = str(policy.get("source") or "").strip()
    known = (
        source == SOURCE_EMAIL
        or source == SOURCE_GROUPS
        or source == SOURCE_ROLES
        or source.startswith(SOURCE_PROFILE_PREFIX)
    )
    grants = policy.get("grants") or []
    # A source that was typed but is not recognised gets the specific message;
    # only an *empty* source falls through to the "choose something" prompt.
    if source and not known:
        raise ValueError(
            f"'{source}' is not a known identity source. Use "
            f"'profile.<attribute>', '{SOURCE_EMAIL}', '{SOURCE_GROUPS}' or "
            f"'{SOURCE_ROLES}'."
        )
    if not known and not grants:
        raise ValueError(
            "Choose what to match the column against: a profile attribute, the "
            "user's email, their groups or roles — or add at least one grant."
        )

    op = (policy.get("op") or OP_IN).lower()
    if op != OP_IN:
        raise ValueError(f"Unsupported operator '{op}'. Only 'in' is available.")

    for i, grant in enumerate(grants):
        if not isinstance(grant, dict):
            raise ValueError(f"Grant {i + 1} is malformed.")
        if (grant.get("principal_type") or "").lower() not in ("user", "group", "role"):
            raise ValueError(
                f"Grant {i + 1} must target a user, a group or a role."
            )
        if not str(grant.get("principal_id") or "").strip():
            raise ValueError(f"Grant {i + 1} names no principal.")
