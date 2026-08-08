"""Row- and column-level security on a report query's results.

A report creator writes a query once; everyone they share the report (or its
artifact) with runs that same saved code. This module is what makes one saved
query return a different slice to each of them.

**Enforcement is per `execute_query` return value.** Not per step, not per
report. One step's generated code routinely calls `execute_query` several times
— once per table, per join leg, per data source — and a report holds many
queries, each with its own policy. There is no single "the query" to intercept,
so the filter runs on every individual call's result frame, evaluated on its own
terms. It rides on `QueryCapturingClientWrapper`
(app/ai/code_execution/code_execution.py), which already wraps every client on
every execution path and already routes the `.query()` alias back through its
own `execute_query` — so there is no second entry point that could slip past.

**Filtering happens before generated code touches the frame.** That ordering is
load-bearing, and more so for columns than for rows: model-written code doing
`df.rename(columns={'salary': 'sal'})` would walk a restricted column straight
past any mask applied further downstream. Masking at the boundary closes that by
construction rather than by hoping the rename never happens.

**The policy author and the identity it compiles against are different people.**
The report creator writes the policy; the viewer is who it resolves for. That is
deliberately independent of `Report.shared_run_identity`, which chooses whose
*database credentials* run the query — a report can legitimately run under the
creator's credentials and still owe each viewer their own row slice.

**Rows and columns compile from the same grant vocabulary.** The row policy is
`app.data_sources.fast.rls`, reused verbatim (same shapes, same compiler, same
identity sources); columns add a mask on top and share
`rls.principal_matches`, because two implementations of "does this grant name
this identity" would be two places for a matching bug to hide.

Unresolved means denied, throughout. An identity with no value for the attribute
a policy binds to gets zero rows, and a restricted column with no matching grant
is masked — see the module docstring on `fast/rls.py` for why that default is
the only safe one.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.data_sources.fast import rls

logger = logging.getLogger(__name__)

# Masked cells read as empty rather than as a marker string. A sentinel like
# "***" would be a value: it changes a column's dtype to object, breaks any
# numeric aggregation the widget performs on it, and renders as literal text in
# a chart axis. Null is what "you may not see this" already means everywhere
# downstream.
MASK_VALUE = None


@dataclass
class ColumnMask:
    """Which columns this identity may not see."""

    masked: List[str] = field(default_factory=list)
    reason: str = ""

    @property
    def unrestricted(self) -> bool:
        return not self.masked


UNMASKED = ColumnMask(reason="no policy")


def compile_column_policy(
    policy: Optional[Dict[str, Any]],
    identity: rls.Identity,
) -> ColumnMask:
    """Resolve which restricted columns `identity` has no grant for.

    An allowlist of RESTRICTIONS, not of columns: a column named in the policy
    is masked unless a grant matches, and every column the policy does not name
    stays visible. That inversion is what keeps the feature usable — a creator
    protecting `salary` should not have to enumerate the other forty columns.

    Never raises. A malformed policy masks (the closed direction) rather than
    being skipped.
    """
    if not policy:
        return ColumnMask(reason="no policy")

    entries = policy.get("columns")
    if not isinstance(entries, list):
        return ColumnMask(reason="policy names no columns")

    masked: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            # A malformed entry names a column we cannot verify a grant for.
            # Skipping it would un-restrict whatever it was meant to protect,
            # but we do not know the name, so there is nothing to mask — log
            # loudly instead of failing silently.
            logger.warning("cls.malformed_entry", extra={"entry": repr(entry)[:200]})
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        grants = entry.get("grants") or []
        granted = any(
            isinstance(g, dict) and rls.principal_matches(g, identity)
            for g in grants
        )
        if not granted:
            masked.append(name)

    if not masked:
        return ColumnMask(reason="every restricted column is granted")
    return ColumnMask(masked=masked, reason="no grant for restricted column(s)")


def validate_column_policy(
    policy: Optional[Dict[str, Any]],
    available_columns: Sequence[str] = (),
) -> None:
    """Reject a column policy at save time, with a message a creator can act on.

    Not the enforcement boundary — the query's result columns can change under a
    saved policy — but a creator should learn about a typo when they press Save
    rather than by a colleague reporting a blank column.
    """
    if not isinstance(policy, dict) or not policy:
        raise ValueError("A column policy is required when column security is enabled.")
    entries = policy.get("columns")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Choose at least one column to restrict.")

    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Restricted column {i + 1} is malformed.")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"Restricted column {i + 1} has no name.")
        if name.lower() in seen:
            raise ValueError(f"'{name}' is listed twice.")
        seen.add(name.lower())
        if available_columns and name not in set(available_columns):
            raise ValueError(
                f"'{name}' is not a column of this query's result. "
                f"Available: {', '.join(sorted(available_columns)[:20])}"
            )
        for j, grant in enumerate(entry.get("grants") or []):
            if not isinstance(grant, dict):
                raise ValueError(f"Grant {j + 1} on '{name}' is malformed.")
            if (grant.get("principal_type") or "").lower() not in ("user", "group", "role"):
                raise ValueError(
                    f"Grant {j + 1} on '{name}' must target a user, a group or a role."
                )
            if not str(grant.get("principal_id") or "").strip():
                raise ValueError(f"Grant {j + 1} on '{name}' names no principal.")


def _resolve_column(frame_columns: Sequence[str], name: str) -> Optional[str]:
    """Find `name` among a result frame's columns.

    Exact match first, then case-insensitive: a creator picks the column from
    the query's own result, but the same query re-run against a different
    dialect can come back with a different case (Oracle upper-cases unquoted
    identifiers, Snowflake likewise). Failing to match there would silently
    un-restrict the column, so the looser comparison is the safer one.
    """
    if name in frame_columns:
        return name
    lowered = name.lower()
    for c in frame_columns:
        if str(c).lower() == lowered:
            return c
    return None


@dataclass
class QueryAccessPolicy:
    """A compiled row filter and column mask, ready to apply to result frames.

    Compiled once per step execution against one identity, then applied to
    every frame that step's code fetches.
    """

    row_filter: Optional[rls.Filter] = None
    column_mask: Optional[ColumnMask] = None
    # Identifies the query in logs. Never used in a comparison.
    query_id: Optional[str] = None

    @property
    def active(self) -> bool:
        row = self.row_filter is not None and not self.row_filter.unrestricted
        col = self.column_mask is not None and not self.column_mask.unrestricted
        return row or col

    def apply(self, result: Any) -> Any:
        """Filter one `execute_query` result.

        Rows first, then columns: the row filter binds to a column that may
        itself be restricted, and masking it first would leave nothing to filter
        on.

        A non-tabular result under an active policy raises rather than passing
        through. A creator only attaches a policy to a query that returns a
        table, so this shape is anomalous — and "we could not filter it, so we
        served it" is the one outcome this module must never produce.
        """
        if not self.active:
            return result

        import pandas as pd

        if not isinstance(result, pd.DataFrame):
            raise RuntimeError(
                "Refusing to serve a non-tabular result under a row/column "
                f"security policy (got {type(result).__name__}). The policy on "
                "this query expects a table."
            )

        df = result
        f = self.row_filter
        if f is not None and not f.unrestricted:
            df = self._apply_rows(df, f)

        m = self.column_mask
        if m is not None and not m.unrestricted:
            df = self._apply_columns(df, m)
        return df

    @staticmethod
    def _apply_rows(df, f: rls.Filter):
        if f.deny_all:
            # An empty frame with the SAME columns, never a raised error: the
            # widget renders "no rows" honestly, where a missing-column crash
            # would read as a broken report rather than an empty slice.
            return df.iloc[0:0]

        col = _resolve_column(list(df.columns), f.column or "")
        if col is None:
            # The query no longer returns the column the policy filters on. We
            # cannot filter on what is gone, so nobody sees anything — the same
            # reading `compile_policy` already takes for a vanished column.
            logger.warning(
                "query_rls.column_missing",
                extra={"column": f.column, "columns": [str(c) for c in df.columns][:20]},
            )
            return df.iloc[0:0]

        allowed = set(f.allowed_values or [])
        # Compared as strings because the identity side is always strings (an
        # IdP claim, a group name); a numeric column would never match
        # otherwise. NaN stringifies to something outside `allowed`, so null
        # cells fall out — the closed direction.
        return df[df[col].astype(str).isin(allowed)]

    @staticmethod
    def _apply_columns(df, m: ColumnMask):
        resolved = [c for c in (_resolve_column(list(df.columns), n) for n in m.masked) if c]
        if not resolved:
            return df
        # Copy before writing: the frame may be a slice of the row filter's
        # result, and assigning into a view raises SettingWithCopyWarning (and
        # on newer pandas, silently does nothing).
        df = df.copy()
        for c in resolved:
            df[c] = MASK_VALUE
        return df


def compile_for_query(query: Any, identity: rls.Identity) -> Optional[QueryAccessPolicy]:
    """The policy to enforce on `query` for `identity`, or None if unprotected.

    Deliberately NOT license-checked. Authoring a policy is an enterprise
    feature; enforcing one that is already saved is not, because skipping the
    filter on a lapsed license would turn a billing event into a data leak.
    """
    if query is None:
        return None

    row_filter = None
    if getattr(query, "rls_enabled", False):
        row_filter = rls.compile_policy(
            getattr(query, "rls_policy", None),
            identity,
            # No column list: unlike a materialized relation, a live query's
            # result shape is not known until it runs. The column is checked
            # against the real frame in `_apply_rows`, which denies if it is
            # absent — the same outcome, one step later.
            available_columns=(),
            default_deny=bool(getattr(query, "rls_default_deny", True)),
            mode=getattr(query, "rls_mode", None) or rls.MODE_ATTRIBUTE,
        )

    column_mask = None
    if getattr(query, "cls_enabled", False):
        column_mask = compile_column_policy(getattr(query, "cls_policy", None), identity)

    if row_filter is None and column_mask is None:
        return None
    policy = QueryAccessPolicy(
        row_filter=row_filter,
        column_mask=column_mask,
        query_id=str(getattr(query, "id", "") or "") or None,
    )
    if policy.active:
        logger.info(
            "query_access_policy.active",
            extra={
                "query_id": policy.query_id,
                "user_id": identity.user_id,
                "rows": getattr(row_filter, "reason", None),
                "masked_columns": getattr(column_mask, "masked", None),
            },
        )
    return policy


async def compile_for_step(
    db,
    step: Any,
    viewing_user: Any,
    organization_id: str,
) -> Optional[QueryAccessPolicy]:
    """Resolve the policy for the query a step belongs to, for one viewer.

    Loads the Query with an explicit SELECT rather than through `step.query`.
    A report rerun loads its steps under `lazyload("*")` (StepService.
    _load_step_for_rerun) to avoid the selectin cascade, so the relationship is
    unpopulated there and touching it on an async session raises. Reading the
    row directly works under every loading strategy — and getting this wrong
    fails in the direction of serving unfiltered data, which is exactly the
    class of bug this feature exists to prevent.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import lazyload

    from app.models.query import Query as QueryModel

    query_id = getattr(step, "query_id", None)
    if not query_id:
        return None

    query = (await db.execute(
        select(QueryModel).options(lazyload("*")).where(QueryModel.id == str(query_id))
    )).scalar_one_or_none()
    if query is None:
        return None
    if not (getattr(query, "rls_enabled", False) or getattr(query, "cls_enabled", False)):
        return None

    from app.services.rls_identity_service import resolve_identity

    identity = await resolve_identity(db, viewing_user, str(organization_id))
    return compile_for_query(query, identity)
