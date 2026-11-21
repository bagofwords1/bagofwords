"""Organization-scope query guard (detection only).

Flags ORM SELECT / UPDATE / DELETE statements that touch an organization-scoped
model without any criterion on that model's organization column, while a
request is being served for a specific organization. It never rewrites a
statement: in ``warn`` mode it logs each distinct finding once and keeps it in
an in-process registry (``get_findings()``); in ``enforce`` mode it raises
``OrgScopeViolation`` instead. ``off`` registers nothing.

What is checked
- Only inside an organization-scoped request, i.e. when
  ``app.core.request_org.current_request_organization_id()`` is set (done by
  ``get_current_organization``). Sign-in, startup, migrations, scheduler jobs
  and anything else without a request organization are skipped entirely.
- Every FROM entity whose table has an organization column
  (``organization_id``, or any foreign key to ``organizations.id``; for
  ``organizations`` itself, its ``id``).
- An entity passes when its organization column is compared with a value in a
  WHERE / JOIN ON / HAVING criterion, or when it is tied by a criterion (join
  condition, correlated subquery, ``IN (subquery)``) to an entity that is.
  A join condition between two organization columns ties the entities but
  does not by itself scope either.
  A primary-key-only lookup does not pass: the row must also be shown to
  belong to the caller's organization.
- Relationship and column loads (lazy loads, ``selectinload``, joined eager
  loads, refresh) are skipped: they hang off a row that was already loaded.

Opting out, for code that legitimately reads across organizations or loads by
id and verifies ownership afterwards::

    stmt = select(Model).execution_options(org_scope="system")
    with org_scope_system(): ...

Models without an organization column (reached through a parent) are not
checked yet; they are classified in ``app.core.org_scope_inventory``.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import sysconfig
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import BooleanClauseList, ColumnClause
from sqlalchemy.sql import operators
from sqlalchemy.sql.selectable import (
    AliasedReturnsRows,
    CompoundSelect,
    FromClause,
    Join,
    Select,
    SelectBase,
    TableClause,
)

from app.core.request_org import current_request_organization_id

logger = logging.getLogger(__name__)

MODES = ("off", "warn", "enforce")
SYSTEM_SCOPE = "system"
ORGANIZATIONS_TABLE = "organizations"
# Column names that scope a row to an organization wherever they appear
# (including on a subquery that re-exports them).
_ORG_COLUMN_NAMES = frozenset({"organization_id", "org_id"})

_ANALYSIS_CACHE_MAX = 4000
_FINDINGS_MAX = 5000
_STATEMENT_MAX_CHARS = 400


class OrgScopeViolation(RuntimeError):
    """Raised in ``enforce`` mode for a statement the guard would flag."""


@dataclass
class Finding:
    operation: str            # select | update | delete
    models: Tuple[str, ...]   # org-scoped models read without an org criterion
    statement: str            # normalized, truncated SQL
    call_site: str            # first application frame: path:line (function)
    count: int = 1


_mode: str = "off"
_installed: bool = False
_system_scope: ContextVar[bool] = ContextVar("bow_org_scope_system", default=False)
_lock = threading.Lock()
# statement cache key -> None (passes) | (operation, models, statement text)
_analysis_cache: Dict[object, Optional[Tuple[str, Tuple[str, ...], str]]] = {}
_findings: Dict[Tuple[str, str], Finding] = {}
_scoped_tables: Optional[Dict[str, Tuple[str, frozenset]]] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@contextmanager
def org_scope_system() -> Iterator[None]:
    """Mark the enclosed queries as deliberately not organization-scoped."""
    token = _system_scope.set(True)
    try:
        yield
    finally:
        _system_scope.reset(token)


def get_mode() -> str:
    return _mode


def configured_mode() -> str:
    """The mode from ``security.org_scope_guard`` (``off`` if unreadable)."""
    try:
        from app.settings.config import settings

        mode = settings.bow_config.security.org_scope_guard
    except Exception:  # config not loadable (scripts, partial imports)
        return "off"
    return mode if mode in MODES else "off"


def install(mode: Optional[str] = None) -> str:
    """Register (or, for ``off``, remove) the session hook. Idempotent."""
    global _mode, _installed
    mode = configured_mode() if mode is None else mode
    if mode not in MODES:
        raise ValueError(f"org_scope_guard mode must be one of {MODES}, got {mode!r}")
    with _lock:
        if mode == "off":
            if _installed:
                event.remove(Session, "do_orm_execute", _on_orm_execute)
                _installed = False
        elif not _installed:
            # On the Session class: the app builds sessions from several
            # factories (and ad-hoc ``AsyncSession(bind=...)``); an AsyncSession
            # runs its statements through a plain Session underneath.
            event.listen(Session, "do_orm_execute", _on_orm_execute)
            _installed = True
        _mode = mode
    return mode


def uninstall() -> None:
    install("off")


def is_installed() -> bool:
    return _installed


def get_findings() -> List[Finding]:
    """Distinct findings recorded in this process, most frequent first."""
    with _lock:
        return sorted(_findings.values(), key=lambda f: (-f.count, f.call_site, f.statement))


def reset_findings() -> None:
    with _lock:
        _findings.clear()


def findings_markdown(findings: Optional[List[Finding]] = None) -> str:
    """The findings as a review list grouped by call site, busiest first."""
    findings = get_findings() if findings is None else findings
    by_site: Dict[str, List[Finding]] = {}
    for finding in findings:
        by_site.setdefault(finding.call_site, []).append(finding)
    sites = sorted(by_site.items(), key=lambda kv: (-sum(f.count for f in kv[1]), kv[0]))
    lines = [
        "# Organization-scope guard findings",
        "",
        f"{len(findings)} distinct statements at {len(sites)} call sites, "
        f"{sum(f.count for f in findings)} executions.",
        "",
    ]
    for site, group in sites:
        lines.append(f"## `{site}` ({sum(f.count for f in group)} executions)")
        lines.append("")
        for finding in sorted(group, key=lambda f: -f.count):
            lines.append(
                f"- {finding.count}x {finding.operation} **{', '.join(finding.models)}**: "
                f"`{finding.statement}`"
            )
        lines.append("")
    return "\n".join(lines)


def reset_caches() -> None:
    """Forget analysed statements and the model map (tests that add models)."""
    global _scoped_tables
    with _lock:
        _analysis_cache.clear()
        _scoped_tables = None


# ---------------------------------------------------------------------------
# Session hook
# ---------------------------------------------------------------------------

def _on_orm_execute(state) -> None:
    # Hot path: everything before the cache lookup must stay trivial.
    if current_request_organization_id() is None or _system_scope.get():
        return
    if state.is_column_load or state.is_relationship_load:
        return
    if state.is_select:
        operation = "select"
    elif state.is_update:
        operation = "update"
    elif state.is_delete:
        operation = "delete"
    else:
        return
    statement = state.statement
    if (
        state.execution_options.get("org_scope") == SYSTEM_SCOPE
        or statement._execution_options.get("org_scope") == SYSTEM_SCOPE
    ):
        return
    if operation != "select" and state.is_executemany:
        return  # bulk UPDATE/DELETE by primary key of rows already in hand

    # SQLAlchemy memoizes the key on the statement and needs it anyway to
    # look up the compiled form, so this adds a dict probe, not a second walk.
    try:
        cache_key = statement._generate_cache_key()
        key = cache_key.key if cache_key is not None else None
    except Exception:
        key = None
    if key is not None:
        try:
            result = _analysis_cache[key]
        except KeyError:
            result = _analyze_safely(operation, statement)
            if len(_analysis_cache) >= _ANALYSIS_CACHE_MAX:
                _analysis_cache.clear()
            _analysis_cache[key] = result
    else:
        result = _analyze_safely(operation, statement)
    if result is None:
        return
    _report(result)


def _report(result: Tuple[str, Tuple[str, ...], str]) -> None:
    operation, models, text = result
    call_site = _call_site()
    dedupe_key = (text, call_site)
    with _lock:
        finding = _findings.get(dedupe_key)
        if finding is not None:
            finding.count += 1
            is_new = False
        else:
            is_new = True
            if len(_findings) < _FINDINGS_MAX:
                _findings[dedupe_key] = Finding(operation, models, text, call_site)
    if _mode == "enforce":
        raise OrgScopeViolation(
            f"{operation} on {', '.join(models)} has no organization criterion "
            f"(at {call_site}): {text}"
        )
    if is_new:
        logger.warning(
            "org_scope_guard: %s on %s without an organization criterion at %s | %s",
            operation, ",".join(models), call_site, text,
            extra={
                "org_scope_guard": {
                    "operation": operation,
                    "models": list(models),
                    "call_site": call_site,
                    "statement": text,
                }
            },
        )


# ---------------------------------------------------------------------------
# Statement analysis (runs once per distinct statement shape)
# ---------------------------------------------------------------------------

def _analyze_safely(operation: str, statement) -> Optional[Tuple[str, Tuple[str, ...], str]]:
    try:
        models = _unscoped_models(operation, statement)
    except Exception:  # never let detection break a query
        logger.debug("org_scope_guard: could not analyse statement", exc_info=True)
        return None
    if not models:
        return None
    return operation, models, _statement_text(statement)


def _org_scoped_tables() -> Dict[str, Tuple[str, frozenset]]:
    """table name -> (model name, organization column names)."""
    global _scoped_tables
    tables = _scoped_tables
    if tables is None:
        from app.models.base import Base

        tables = {}
        for mapper in Base.registry.mappers:
            table = mapper.local_table
            name = getattr(table, "name", None)
            if name is None or name in tables:
                continue
            columns = org_columns_of(table)
            if columns:
                tables[name] = (mapper.class_.__name__, columns)
        _scoped_tables = tables
    return tables


def org_columns_of(table) -> frozenset:
    """Columns that tie a row of ``table`` to its organization."""
    if table.name == ORGANIZATIONS_TABLE:
        return frozenset({"id"})
    names = {c.name for c in table.columns if c.name == "organization_id"}
    names.update(
        fk.parent.name for fk in table.foreign_keys
        if fk.column.table.name == ORGANIZATIONS_TABLE
    )
    return frozenset(names)


class _Analysis:
    """Union-find over the statement's FROM elements.

    Elements are keyed by ``hash()``: an ORM-annotated table/alias hashes like
    the element it wraps, so a column's ``.table`` and the FROM entry agree.
    """

    def __init__(self, scoped: Dict[str, Tuple[str, frozenset]]):
        self.scoped = scoped
        self.parent: Dict[int, int] = {}
        self.entities: Dict[int, str] = {}          # node -> table name
        self.column_refs: List[Tuple[int, str]] = []  # criteria columns

    def find(self, node: int) -> int:
        parent = self.parent
        root = parent.setdefault(node, node)
        while root != parent[root]:
            parent[root] = parent[parent[root]]
            root = parent[root]
        return root

    def union(self, nodes) -> None:
        nodes = list(nodes)
        if not nodes:
            return
        first = self.find(nodes[0])
        for other in nodes[1:]:
            self.parent[self.find(other)] = first

    # -- FROM side ---------------------------------------------------------
    def from_(self, element) -> List[int]:
        if isinstance(element, Join):
            nodes = self.from_(element.left) + self.from_(element.right)
            if element.onclause is not None:
                self.criteria(element.onclause)
            return nodes
        if isinstance(element, TableClause):
            node = hash(element)
            self.find(node)
            if element.name in self.scoped:
                self.entities[node] = element.name
            return [node]
        if isinstance(element, AliasedReturnsRows):  # alias, subquery, CTE, lateral
            node = hash(element)
            self.find(node)
            inner = element.element
            if isinstance(inner, TableClause):
                if inner.name in self.scoped:
                    self.entities[node] = inner.name
            elif isinstance(inner, SelectBase):
                self.union([node] + self.select(inner))
            elif isinstance(inner, FromClause):
                self.union([node] + self.from_(inner))
            return [node]
        inner = getattr(element, "element", None)  # FromGrouping and friends
        if isinstance(inner, SelectBase):
            return self.select(inner)
        if isinstance(inner, FromClause):
            return self.from_(inner)
        return []

    def select(self, statement) -> List[int]:
        if isinstance(statement, CompoundSelect):
            nodes: List[int] = []
            for part in statement.selects:
                nodes.extend(self.select(part))
            return nodes
        if not isinstance(statement, Select):
            inner = getattr(statement, "element", None)  # SelectStatementGrouping
            return self.select(inner) if isinstance(inner, SelectBase) else []
        froms = _user_froms(statement)
        nodes = []
        for from_element in froms:
            nodes.extend(self.from_(from_element))
        for criterion in statement._where_criteria:
            self.criteria(criterion)
        for criterion in statement._having_criteria:
            self.criteria(criterion)
        for column in statement._raw_columns:
            if not isinstance(column, FromClause):
                self.expression(column)  # scalar subqueries
        return nodes

    # -- criteria side -----------------------------------------------------
    def criteria(self, expression) -> None:
        if isinstance(expression, BooleanClauseList) and expression.operator is operators.and_:
            for clause in expression.clauses:
                self.criteria(clause)
            return
        columns: List[Tuple[int, str]] = []
        nodes = self.expression(expression, columns)
        if len(set(nodes)) == 1:
            # Columns of a single FROM element compared with values. A
            # comparison between two elements (a join condition) ties them
            # together but pins neither to an organization.
            self.column_refs.extend(columns)
        self.union(nodes)

    def expression(self, element, columns: Optional[List[Tuple[int, str]]] = None) -> List[int]:
        """FROM elements referenced by ``element``; nested selects are analysed
        on their own and contribute their FROM elements."""
        nodes: List[int] = []
        stack = [element]
        while stack:
            current = stack.pop()
            if isinstance(current, SelectBase):
                nodes.extend(self.select(current))
            elif isinstance(current, ColumnClause):
                table = current.table
                if table is not None:
                    node = hash(table)
                    self.find(node)
                    nodes.append(node)
                    if columns is not None:
                        columns.append((node, current.name))
            elif isinstance(current, FromClause):
                nodes.extend(self.from_(current))
            else:
                stack.extend(current.get_children())
        return nodes

    # -- verdict -----------------------------------------------------------
    def unscoped(self) -> Tuple[str, ...]:
        anchored = set()
        for node, column in self.column_refs:
            table = self.entities.get(node)
            if column in _ORG_COLUMN_NAMES or (
                table is not None and column in self.scoped[table][1]
            ):
                anchored.add(self.find(node))
        return tuple(sorted({
            self.scoped[table][0]
            for node, table in self.entities.items()
            if self.find(node) not in anchored
        }))


def _user_froms(statement):
    """The statement's FROM list with ORM relationship joins resolved, but
    without the joins added for eagerly loaded relationships (``lazy="joined"``
    / ``joinedload``): those are relationship loads off the selected row."""
    try:
        compile_state = statement._compile_state_factory(statement, statement._default_compiler())
        eager = {id(join) for join in getattr(compile_state, "eager_joins", {}).values()}
        froms = [f for f in compile_state.from_clauses if id(f) not in eager]
        if froms:
            return froms
    except Exception:
        pass
    try:
        return statement.get_final_froms()
    except Exception:
        return statement.froms


def _unscoped_models(operation: str, statement) -> Tuple[str, ...]:
    analysis = _Analysis(_org_scoped_tables())
    if operation == "select":
        analysis.select(statement)
    else:
        analysis.from_(statement.table)
        for criterion in statement._where_criteria:
            analysis.criteria(criterion)
    return analysis.unscoped()


_WHITESPACE = re.compile(r"\s+")
_SELECT_LIST = re.compile(r"^SELECT (.{80,}?) FROM ", re.S)


def _statement_text(statement) -> str:
    try:
        text = _WHITESPACE.sub(" ", str(statement)).strip()
    except Exception:
        return f"<{type(statement).__name__}>"
    text = _SELECT_LIST.sub("SELECT ... FROM ", text, count=1)
    if len(text) > _STATEMENT_MAX_CHARS:
        # Keep both ends: the entities are at the start, the criteria at the end.
        half = _STATEMENT_MAX_CHARS // 2
        text = text[:half] + " ... " + text[-half:]
    return text


# ---------------------------------------------------------------------------
# Call site
# ---------------------------------------------------------------------------

_THIS_FILE = os.path.abspath(__file__)
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_FILE)))
_STDLIB = sysconfig.get_paths().get("stdlib") or ""


def _is_application_file(filename: str) -> bool:
    if not filename or filename.startswith("<") or filename == _THIS_FILE:
        return False
    if "site-packages" in filename or "dist-packages" in filename:
        return False
    return not (_STDLIB and filename.startswith(_STDLIB))


def _format_frame(frame) -> str:
    filename = frame.f_code.co_filename
    if filename.startswith(_BACKEND_ROOT + os.sep):
        filename = filename[len(_BACKEND_ROOT) + 1:]
    return f"{filename}:{frame.f_lineno} ({frame.f_code.co_name})"


def _first_application_frame(frame) -> Optional[str]:
    while frame is not None:
        if _is_application_file(frame.f_code.co_filename):
            return _format_frame(frame)
        frame = frame.f_back
    return None


def _call_site() -> str:
    found = _first_application_frame(sys._getframe(2))
    if found is None:
        # AsyncSession runs the statement in a greenlet whose stack ends at the
        # greenlet boundary; the awaiting application code is on the parent's.
        try:
            import greenlet

            parent = greenlet.getcurrent().parent
            while found is None and parent is not None:
                found = _first_application_frame(parent.gr_frame)
                parent = parent.parent
        except Exception:
            found = None
    return found or "<unknown>"
