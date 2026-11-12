"""Row-level security on accelerated relations.

A custom table is materialized once with a shared credential, so every agent
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
    """The compiled result of a policy against one identity.

    Carries both halves of relation security: which rows (`allowed_values` /
    `deny_all` / `column`) and which columns (`allowed_columns`). Either half
    may be unrestricted independently — a relation can filter rows, project
    columns, both, or neither.
    """

    # None → no row restriction; the artifact is served attached, without a copy.
    allowed_values: Optional[List[str]] = None
    # True → zero rows, whatever the values say.
    deny_all: bool = False
    column: Optional[str] = None
    # None → every column. A list → the only columns the identity may see, in
    # the relation's own column order. Column security projects to these at read
    # time, in the same catalog that filters rows, so it cannot be bypassed.
    allowed_columns: Optional[List[str]] = None
    reason: str = ""

    @property
    def unrestricted(self) -> bool:
        return (
            not self.deny_all
            and self.allowed_values is None
            and self.allowed_columns is None
        )


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


# ---------------------------------------------------------------------------
# Column-level security
# ---------------------------------------------------------------------------
#
# The symmetry with rows is deliberate but not total. A row policy governs ONE
# dimension (the filter column) and returns everything else about the row; a
# column policy governs the named sensitive columns and returns everything else
# about the row's shape. Both are fail-closed on what they govern: a row whose
# identity does not resolve yields nothing, a restricted column that no grant
# names is hidden.
#
# The one structural difference: rows have a `source` — an identity attribute
# that resolves a per-user value automatically, so one rule serves everyone. A
# column name is not a value that lives on the identity, so there is nothing to
# resolve automatically; grants name the columns explicitly per principal.


def compile_column_policy(
    policy: Optional[Dict[str, Any]],
    identity: Identity,
    *,
    available_columns: Sequence[str] = (),
) -> Optional[List[str]]:
    """The columns `identity` may see, in `available_columns` order.

    Returns None when nothing is restricted (serve every column) and a list
    otherwise. An empty list means every column is restricted and none was
    granted — the caller maps that to a full relation-level deny, because a
    projection with no columns is not a query.

    Never raises: like the row compiler, every way the policy can be malformed
    resolves to *more* hidden, never less. A restricted column that is not in
    the materialized relation is dropped (it cannot leak), and a grant naming a
    column that is not restricted is ignored.
    """
    if not policy:
        return None

    available = [str(c) for c in (available_columns or [])]
    available_set = set(available)
    # Only restrict columns that actually exist — a policy naming a column the
    # relation no longer has should not silently hide a *different* one, and a
    # stale name must not deny the whole table.
    restricted = {
        str(c) for c in (policy.get("restricted_columns") or []) if c
    } & available_set
    if not restricted:
        return None

    granted: Set[str] = set()
    for grant in policy.get("grants") or []:
        if not isinstance(grant, dict) or not _principal_matches(grant, identity):
            continue
        cols = _as_values(grant.get("columns"))
        if WILDCARD in cols:
            granted |= restricted
        else:
            granted |= {c for c in cols if c in restricted}

    # Everything not restricted stays; a restricted column survives only if it
    # was granted. Order follows the relation so the projection is stable.
    return [c for c in available if c not in restricted or c in granted]


def validate_column_policy(
    policy: Optional[Dict[str, Any]],
    available_columns: Sequence[str] = (),
) -> None:
    """Reject a column policy at save time. Raises ValueError (admin-readable).

    As with rows, this is a convenience, not the enforcement boundary — the
    relation's columns can change afterwards and `compile_column_policy` copes.
    It exists so a typo surfaces on Save.
    """
    if not isinstance(policy, dict) or not policy:
        raise ValueError("A column policy is required when column security is enabled.")

    restricted = policy.get("restricted_columns") or []
    if not isinstance(restricted, list) or not restricted:
        raise ValueError("Choose at least one column to restrict.")

    available_set = set(str(c) for c in (available_columns or []))
    for col in restricted:
        name = str(col or "").strip()
        if not name:
            raise ValueError("A restricted column has no name.")
        if not _COLUMN_RE.match(name):
            raise ValueError(f"'{name}' is not a valid column name.")
        if available_set and name not in available_set:
            raise ValueError(
                f"'{name}' is not a column of this query's result. "
                f"Available: {', '.join(sorted(available_set)[:20])}"
            )

    for i, grant in enumerate(policy.get("grants") or []):
        if not isinstance(grant, dict):
            raise ValueError(f"Grant {i + 1} is malformed.")
        if (grant.get("principal_type") or "").lower() not in ("user", "group", "role"):
            raise ValueError(f"Grant {i + 1} must target a user, a group or a role.")
        if not str(grant.get("principal_id") or "").strip():
            raise ValueError(f"Grant {i + 1} names no principal.")
        if not _as_values(grant.get("columns")):
            raise ValueError(
                f"Grant {i + 1} names no columns — list the columns it unlocks, "
                f"or '{WILDCARD}' for all restricted columns."
            )
