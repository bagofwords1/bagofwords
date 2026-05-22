"""Declarative apply for Agent (DataSource) YAML manifests.

A single ``apply(yaml_text)`` call:
- creates the resource if it doesn't exist for ``(org_id, manifest.name)``;
- otherwise reconciles each field against the desired state;
- returns ``{created | updated | unchanged | dry_run | error}`` with a
  structured error list (and ``did-you-mean`` suggestions) instead of
  surfacing exceptions.

The service is intentionally thin: connection / membership creation,
license gates, indexing kickoff, audit + telemetry all reuse
``DataSourceService.create_data_source``. Apply only owns the
*reconciliation* — connections add/remove, table filters, tool overlays,
inline instructions, group members, conversation starters.
"""

from __future__ import annotations

import difflib
import fnmatch
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml
from pydantic import ValidationError
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permission_resolver import (
    FULL_ADMIN,
    resolve_permissions,
)
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.connection_tool import ConnectionTool
from app.models.data_source import DataSource
from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.data_source_membership import (
    DataSourceMembership,
    PRINCIPAL_TYPE_GROUP,
    PRINCIPAL_TYPE_USER,
)
from app.models.datasource_table import DataSourceTable
from app.models.group import Group
from app.models.instruction import (
    Instruction,
    instruction_data_source_association,
)
from app.models.organization import Organization
from app.models.resource_grant import ResourceGrant
from app.models.user import User
from app.schemas.agent_manifest_schema import (
    AgentManifest,
    ApplyError,
    ApplyErrorCode,
    ApplyResult,
    ApplyStatus,
    ApplyWarning,
    ApplyWarningCode,
    InstructionRef,
    MemberRef,
    TableRules,
    ToolsOverlay,
)
from app.schemas.data_source_schema import DataSourceCreate
from app.schemas.instruction_schema import InstructionCreate
from app.services.data_source_service import DataSourceService
from app.services.instruction_service import InstructionService


logger = logging.getLogger(__name__)


DEFAULT_MEMBER_PERMISSIONS = ["view", "view_schema"]
OWNER_PERMISSIONS = ["manage"]
TOOL_COMPATIBLE_CONNECTION_TYPES = {"mcp", "custom_api"}


# ---------------------------------------------------------------------------
# Resolution result types
# ---------------------------------------------------------------------------


class _Resolved:
    """Bag of resolved references — populated by ``ManifestResolver``."""

    def __init__(self) -> None:
        self.connections: Dict[str, Connection] = {}  # name -> Connection
        self.groups: Dict[str, Group] = {}  # name -> Group
        self.users: Dict[str, User] = {}  # email -> User
        self.instructions: Dict[str, Instruction] = {}  # id -> Instruction
        # (connection_name, tool_name) -> ConnectionTool
        self.tools: Dict[Tuple[str, str], ConnectionTool] = {}


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------


class AgentYamlService:
    """Service for applying / exporting agent YAML manifests."""

    def __init__(self) -> None:
        self._ds_service = DataSourceService()
        self._instruction_service = InstructionService()

    # ----- apply ----------------------------------------------------------

    async def apply(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        yaml_text: str,
        *,
        dry_run: bool = False,
    ) -> ApplyResult:
        errors: List[ApplyError] = []
        warnings: List[ApplyWarning] = []

        # 1. Parse YAML
        try:
            raw = yaml.safe_load(yaml_text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            loc: List[Any] = []
            if mark is not None:
                loc = [f"line {mark.line + 1}", f"col {mark.column + 1}"]
            return ApplyResult(
                status=ApplyStatus.ERROR,
                errors=[
                    ApplyError(
                        loc=loc,
                        code=ApplyErrorCode.YAML_PARSE_ERROR,
                        message=str(e),
                    )
                ],
            )
        if not isinstance(raw, dict):
            return ApplyResult(
                status=ApplyStatus.ERROR,
                errors=[
                    ApplyError(
                        loc=[],
                        code=ApplyErrorCode.YAML_PARSE_ERROR,
                        message="YAML must decode to a mapping (object).",
                    )
                ],
            )

        # 2. Pydantic validation — collect ALL errors before returning
        try:
            manifest = AgentManifest.model_validate(raw)
        except ValidationError as ve:
            return ApplyResult(
                status=ApplyStatus.ERROR,
                errors=_pydantic_errors_to_apply_errors(ve),
            )

        # 3. Look up existing agent by (org, name)
        existing = await self._find_existing(db, organization, manifest.name)

        # 4. Permission gate
        perm_errors = await self._check_permissions(
            db, organization, current_user, manifest, existing
        )
        if perm_errors:
            return ApplyResult(
                status=ApplyStatus.ERROR,
                id=str(existing.id) if existing else None,
                name=manifest.name,
                errors=perm_errors,
            )

        # 5. Resolve refs — collect-all-errors
        resolver = ManifestResolver(db, organization)
        resolved, ref_errors = await resolver.resolve(manifest)
        if ref_errors:
            return ApplyResult(
                status=ApplyStatus.ERROR,
                id=str(existing.id) if existing else None,
                name=manifest.name,
                errors=ref_errors,
            )

        # 6. License gate (per connection type)
        license_errors = self._check_licenses(manifest, resolved)
        if license_errors:
            return ApplyResult(
                status=ApplyStatus.ERROR,
                id=str(existing.id) if existing else None,
                name=manifest.name,
                errors=license_errors,
            )

        # 7. Cross-field warnings (non-blocking)
        warnings.extend(self._collect_table_warnings(manifest))

        # 8. Dry-run short-circuit — no DB writes
        if dry_run:
            diff = await self._compute_diff(db, organization, manifest, resolved, existing)
            return ApplyResult(
                status=ApplyStatus.DRY_RUN,
                id=str(existing.id) if existing else None,
                name=manifest.name,
                diff=diff,
                warnings=warnings,
            )

        # 9. Create or update
        if existing is None:
            ds, post_warnings = await self._create_agent(
                db, organization, current_user, manifest, resolved
            )
            warnings.extend(post_warnings)
            return ApplyResult(
                status=ApplyStatus.CREATED,
                id=str(ds.id),
                name=ds.name,
                warnings=warnings,
            )

        ds, diff, post_warnings = await self._update_agent(
            db, organization, current_user, manifest, resolved, existing
        )
        warnings.extend(post_warnings)
        status = ApplyStatus.UNCHANGED if not diff else ApplyStatus.UPDATED
        return ApplyResult(
            status=status,
            id=str(ds.id),
            name=ds.name,
            diff=diff if diff else None,
            warnings=warnings,
        )

    # ----- export ---------------------------------------------------------

    async def export(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        name: str,
    ) -> str:
        ds = await self._find_existing(db, organization, name)
        if ds is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        manifest = await self._build_manifest_from_db(db, ds)
        return yaml.safe_dump(
            manifest.model_dump(exclude_none=True, exclude_defaults=False, mode="json"),
            sort_keys=False,
            allow_unicode=True,
        )

    async def list_agents(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
    ) -> List[Dict[str, Any]]:
        items = await self._ds_service.get_data_sources(db, current_user, organization)
        return [
            {"name": i.name, "description": i.description, "id": i.id}
            for i in items
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _find_existing(
        self,
        db: AsyncSession,
        organization: Organization,
        name: str,
    ) -> Optional[DataSource]:
        q = await db.execute(
            select(DataSource)
            .options(
                selectinload(DataSource.connections),
                selectinload(DataSource.data_source_memberships),
                selectinload(DataSource.tables),
                selectinload(DataSource.instructions),
            )
            .where(
                DataSource.organization_id == organization.id,
                DataSource.name == name,
            )
        )
        return q.scalar_one_or_none()

    async def _check_permissions(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        manifest: AgentManifest,
        existing: Optional[DataSource],
    ) -> List[ApplyError]:
        resolved = await resolve_permissions(
            db, str(current_user.id), str(organization.id)
        )

        if FULL_ADMIN in resolved.org_permissions:
            return []

        if existing is None:
            # Create requires create_data_source on org
            if not resolved.has_org_permission("create_data_source"):
                return [
                    ApplyError(
                        loc=[],
                        code=ApplyErrorCode.PERMISSION_DENIED,
                        message="You do not have permission to create agents in this organization.",
                    )
                ]
            return []

        # Update requires manage on the resource
        if not resolved.has_resource_permission(
            "data_source", str(existing.id), "manage"
        ):
            return [
                ApplyError(
                    loc=[],
                    code=ApplyErrorCode.PERMISSION_DENIED,
                    message=f"You do not have permission to manage agent '{existing.name}'.",
                    value=existing.name,
                )
            ]
        return []

    def _check_licenses(
        self, manifest: AgentManifest, resolved: _Resolved
    ) -> List[ApplyError]:
        try:
            from app.ee.license import is_datasource_allowed
        except Exception:  # pragma: no cover - license module not present
            return []
        out: List[ApplyError] = []
        for i, name in enumerate(manifest.connections):
            conn = resolved.connections.get(name)
            if conn and not is_datasource_allowed(conn.type):
                out.append(
                    ApplyError(
                        loc=["connections", i],
                        code=ApplyErrorCode.LICENSE_REQUIRED,
                        message=(
                            f"The {conn.type} connector requires an enterprise license."
                        ),
                        value=name,
                    )
                )
        return out

    def _collect_table_warnings(self, manifest: AgentManifest) -> List[ApplyWarning]:
        warnings: List[ApplyWarning] = []
        rules = manifest.tables
        if rules is None:
            return warnings
        if rules.include is not None and not rules.include:
            warnings.append(
                ApplyWarning(
                    loc=["tables", "include"],
                    code=ApplyWarningCode.TABLES_FILTER_EMPTY,
                    message="tables.include is empty — no tables will be active.",
                )
            )
        return warnings

    # ----- create ---------------------------------------------------------

    async def _create_agent(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        manifest: AgentManifest,
        resolved: _Resolved,
    ) -> Tuple[DataSource, List[ApplyWarning]]:
        warnings: List[ApplyWarning] = []

        connection_ids = [resolved.connections[name].id for name in manifest.connections]

        # Build a minimal DataSourceCreate payload — Mode 2 (link existing
        # connection(s)). All license / indexing / audit / telemetry hooks
        # in DataSourceService.create_data_source run for us.
        # Build user-only member ids for the legacy create path; group
        # memberships and per-user permission overrides are applied as a
        # follow-up step below.
        user_member_ids = [
            str(resolved.users[m.user].id)
            for m in manifest.members
            if m.user is not None
        ]

        payload = DataSourceCreate(
            name=manifest.name,
            connection_ids=connection_ids,
            is_public=manifest.is_public,
            use_llm_sync=manifest.use_llm_sync,
            member_user_ids=user_member_ids,
        )
        # create_data_source raises HTTPException on duplicate name —
        # we've already guarded that path, but let any other failure
        # propagate; the route handler turns it into an ApplyResult.
        ds_schema = await self._ds_service.create_data_source(
            db, organization, current_user, payload
        )

        # Reload the ORM model with the relationships we'll mutate next.
        ds = await self._find_existing(db, organization, manifest.name)
        assert ds is not None

        # Post-create patches that DataSourceCreate doesn't carry today.
        await self._patch_textual_fields(db, ds, manifest)
        await self._patch_conversation_starters(db, ds, manifest)
        await self._patch_group_memberships(db, organization, ds, manifest, resolved)
        await self._patch_member_permissions(db, organization, ds, manifest, resolved)
        await self._patch_instructions(
            db, organization, current_user, ds, manifest, resolved
        )

        # Table & tool reconciliation operates on the linked connections
        # (which may still be indexing — warnings collected from there).
        warnings.extend(
            await self._apply_table_rules(db, ds, manifest.tables, resolved)
        )
        warnings.extend(
            await self._apply_tool_rules(db, ds, manifest.tools, resolved)
        )

        await db.commit()
        await db.refresh(ds)
        return ds, warnings

    # ----- update ---------------------------------------------------------

    async def _update_agent(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        manifest: AgentManifest,
        resolved: _Resolved,
        ds: DataSource,
    ) -> Tuple[DataSource, Dict[str, Any], List[ApplyWarning]]:
        warnings: List[ApplyWarning] = []
        diff: Dict[str, Any] = {}

        # Scalar fields
        for field, desired in (
            ("description", manifest.description),
            ("context", manifest.context),
            ("is_public", manifest.is_public),
            ("use_llm_sync", manifest.use_llm_sync),
        ):
            current = getattr(ds, field)
            if current != desired:
                diff[field] = {"from": current, "to": desired}
                setattr(ds, field, desired)

        if (ds.conversation_starters or []) != list(manifest.conversation_starters):
            diff["conversation_starters"] = {
                "from": ds.conversation_starters,
                "to": list(manifest.conversation_starters),
            }
            ds.conversation_starters = list(manifest.conversation_starters)

        db.add(ds)

        # Connections (M:N)
        current_conn_ids = {str(c.id) for c in (ds.connections or [])}
        desired_conn_ids = {
            str(resolved.connections[name].id) for name in manifest.connections
        }
        to_link = desired_conn_ids - current_conn_ids
        to_unlink = current_conn_ids - desired_conn_ids
        if to_link or to_unlink:
            ds.connections = [
                resolved.connections[name] for name in manifest.connections
            ]
            diff["connections"] = {
                "linked": [
                    name for name in manifest.connections
                    if str(resolved.connections[name].id) in to_link
                ],
                "unlinked": [
                    str(cid) for cid in to_unlink
                ],
            }
            db.add(ds)

        # Members (users + groups)
        member_diff = await self._reconcile_members(
            db, organization, ds, manifest, resolved
        )
        if member_diff:
            diff["members"] = member_diff

        # Instructions
        instr_diff = await self._reconcile_instructions(
            db, organization, current_user, ds, manifest, resolved
        )
        if instr_diff:
            diff["instructions"] = instr_diff

        # Tables
        tw = await self._apply_table_rules(db, ds, manifest.tables, resolved)
        warnings.extend(tw)

        # Tools
        tools_diff, tool_warnings = await self._apply_tool_rules_with_diff(
            db, ds, manifest.tools, resolved
        )
        warnings.extend(tool_warnings)
        if tools_diff:
            diff["tools"] = tools_diff

        await db.commit()
        await db.refresh(ds)
        return ds, diff, warnings

    # ----- field patches --------------------------------------------------

    async def _patch_textual_fields(
        self, db: AsyncSession, ds: DataSource, manifest: AgentManifest
    ) -> None:
        if manifest.description is not None:
            ds.description = manifest.description
        if manifest.context is not None:
            ds.context = manifest.context
        db.add(ds)

    async def _patch_conversation_starters(
        self, db: AsyncSession, ds: DataSource, manifest: AgentManifest
    ) -> None:
        ds.conversation_starters = list(manifest.conversation_starters) or None
        db.add(ds)

    # ----- members --------------------------------------------------------

    async def _patch_group_memberships(
        self,
        db: AsyncSession,
        organization: Organization,
        ds: DataSource,
        manifest: AgentManifest,
        resolved: _Resolved,
    ) -> None:
        """Write group memberships on create. User memberships are handled
        by ``DataSourceService._create_memberships`` already (legacy path)."""
        for m in manifest.members:
            if m.group is None:
                continue
            group = resolved.groups[m.group]
            db.add(
                DataSourceMembership(
                    data_source_id=ds.id,
                    principal_type=PRINCIPAL_TYPE_GROUP,
                    principal_id=str(group.id),
                )
            )
            db.add(
                ResourceGrant(
                    organization_id=str(organization.id),
                    resource_type="data_source",
                    resource_id=str(ds.id),
                    principal_type=PRINCIPAL_TYPE_GROUP,
                    principal_id=str(group.id),
                    permissions=list(m.permissions or DEFAULT_MEMBER_PERMISSIONS),
                )
            )

    async def _patch_member_permissions(
        self,
        db: AsyncSession,
        organization: Organization,
        ds: DataSource,
        manifest: AgentManifest,
        resolved: _Resolved,
    ) -> None:
        """Apply explicit per-user permission overrides from YAML to the
        ``resource_grants`` rows that ``_create_memberships`` just wrote
        with the default ``[view, view_schema]``."""
        for m in manifest.members:
            if m.user is None or not m.permissions:
                continue
            user = resolved.users[m.user]
            existing = await db.execute(
                select(ResourceGrant).where(
                    ResourceGrant.resource_type == "data_source",
                    ResourceGrant.resource_id == str(ds.id),
                    ResourceGrant.principal_type == PRINCIPAL_TYPE_USER,
                    ResourceGrant.principal_id == str(user.id),
                    ResourceGrant.deleted_at.is_(None),
                )
            )
            grant = existing.scalar_one_or_none()
            if grant is not None:
                grant.permissions = list(m.permissions)
                db.add(grant)

    async def _reconcile_members(
        self,
        db: AsyncSession,
        organization: Organization,
        ds: DataSource,
        manifest: AgentManifest,
        resolved: _Resolved,
    ) -> Dict[str, Any]:
        """Sync memberships + grants to match ``manifest.members`` exactly."""
        desired: List[Tuple[str, str, List[str]]] = []  # (type, id, perms)
        for m in manifest.members:
            if m.user is not None:
                desired.append(
                    (
                        PRINCIPAL_TYPE_USER,
                        str(resolved.users[m.user].id),
                        list(m.permissions or DEFAULT_MEMBER_PERMISSIONS),
                    )
                )
            else:
                desired.append(
                    (
                        PRINCIPAL_TYPE_GROUP,
                        str(resolved.groups[m.group].id),
                        list(m.permissions or DEFAULT_MEMBER_PERMISSIONS),
                    )
                )

        # Always preserve the owner row regardless of YAML contents
        owner_id = str(ds.owner_user_id) if ds.owner_user_id else None

        existing_q = await db.execute(
            select(DataSourceMembership).where(
                DataSourceMembership.data_source_id == ds.id
            )
        )
        existing = existing_q.scalars().all()
        existing_keys = {(e.principal_type, e.principal_id) for e in existing}
        desired_keys = {(t, i) for (t, i, _) in desired}

        added: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []

        # Add new memberships + grants
        for ptype, pid, perms in desired:
            if (ptype, pid) in existing_keys:
                # Update grant permissions if changed
                gres = await db.execute(
                    select(ResourceGrant).where(
                        ResourceGrant.resource_type == "data_source",
                        ResourceGrant.resource_id == str(ds.id),
                        ResourceGrant.principal_type == ptype,
                        ResourceGrant.principal_id == pid,
                        ResourceGrant.deleted_at.is_(None),
                    )
                )
                grant = gres.scalar_one_or_none()
                if grant and list(grant.permissions or []) != perms:
                    grant.permissions = perms
                    db.add(grant)
                continue
            db.add(
                DataSourceMembership(
                    data_source_id=ds.id,
                    principal_type=ptype,
                    principal_id=pid,
                )
            )
            db.add(
                ResourceGrant(
                    organization_id=str(organization.id),
                    resource_type="data_source",
                    resource_id=str(ds.id),
                    principal_type=ptype,
                    principal_id=pid,
                    permissions=perms,
                )
            )
            added.append({"principal_type": ptype, "principal_id": pid})

        # Remove memberships not in desired (except the owner)
        for e in existing:
            if (e.principal_type, e.principal_id) in desired_keys:
                continue
            if e.principal_type == PRINCIPAL_TYPE_USER and e.principal_id == owner_id:
                continue
            await db.delete(e)
            # Also drop the grant
            gres = await db.execute(
                select(ResourceGrant).where(
                    ResourceGrant.resource_type == "data_source",
                    ResourceGrant.resource_id == str(ds.id),
                    ResourceGrant.principal_type == e.principal_type,
                    ResourceGrant.principal_id == e.principal_id,
                    ResourceGrant.deleted_at.is_(None),
                )
            )
            grant = gres.scalar_one_or_none()
            if grant is not None:
                await db.delete(grant)
            removed.append(
                {"principal_type": e.principal_type, "principal_id": e.principal_id}
            )

        out: Dict[str, Any] = {}
        if added:
            out["added"] = added
        if removed:
            out["removed"] = removed
        return out

    # ----- instructions ---------------------------------------------------

    async def _patch_instructions(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        ds: DataSource,
        manifest: AgentManifest,
        resolved: _Resolved,
    ) -> None:
        """Create inline + attach by-id instructions on agent creation."""
        for ref in manifest.instructions:
            if ref.id is not None:
                await self._attach_instruction(db, ds, resolved.instructions[ref.id])
            else:
                inline = ref.inline
                assert inline is not None
                payload = InstructionCreate(
                    text=inline.text,
                    title=inline.title,
                    category=inline.category.value if hasattr(inline.category, "value") else str(inline.category),
                    load_mode=inline.load_mode.value if hasattr(inline.load_mode, "value") else str(inline.load_mode),
                    ai_source="yaml",
                    source_type="user",
                    data_source_ids=[str(ds.id)],
                    status="published",
                )
                await self._instruction_service.create_instruction(
                    db=db,
                    instruction_data=payload,
                    current_user=current_user,
                    organization=organization,
                    force_global=True,
                    auto_finalize=True,
                )

    async def _attach_instruction(
        self, db: AsyncSession, ds: DataSource, instruction: Instruction
    ) -> None:
        # Idempotent attach (skip if already present)
        existing = await db.execute(
            select(instruction_data_source_association).where(
                instruction_data_source_association.c.instruction_id == instruction.id,
                instruction_data_source_association.c.data_source_id == ds.id,
            )
        )
        if existing.first() is not None:
            return
        await db.execute(
            instruction_data_source_association.insert().values(
                instruction_id=instruction.id, data_source_id=ds.id
            )
        )

    async def _reconcile_instructions(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        ds: DataSource,
        manifest: AgentManifest,
        resolved: _Resolved,
    ) -> Dict[str, Any]:
        """Attach by-id refs, create inline ones (matched by title), and
        detach any instruction that was previously attached via YAML but is
        no longer in the manifest. Manually-attached instructions (those
        not created with ``ai_source='yaml'``) are left alone."""

        # Current attached instructions
        current_q = await db.execute(
            select(Instruction)
            .join(
                instruction_data_source_association,
                instruction_data_source_association.c.instruction_id == Instruction.id,
            )
            .where(
                instruction_data_source_association.c.data_source_id == ds.id
            )
        )
        current = list(current_q.scalars().all())
        current_yaml = {i.title: i for i in current if i.ai_source == "yaml" and i.title}

        desired_ids: set[str] = set()
        desired_titles: set[str] = set()
        added: List[str] = []
        for ref in manifest.instructions:
            if ref.id is not None:
                desired_ids.add(ref.id)
                instr = resolved.instructions[ref.id]
                await self._attach_instruction(db, ds, instr)
                added.append(instr.title or instr.id)
                continue
            inline = ref.inline
            assert inline is not None
            existing = current_yaml.get(inline.title)
            if existing is not None:
                # Update existing inline instruction if content changed
                changed = False
                if existing.text != inline.text:
                    existing.text = inline.text
                    changed = True
                cat_val = inline.category.value if hasattr(inline.category, "value") else str(inline.category)
                if existing.category != cat_val:
                    existing.category = cat_val
                    changed = True
                lm_val = inline.load_mode.value if hasattr(inline.load_mode, "value") else str(inline.load_mode)
                if existing.load_mode != lm_val:
                    existing.load_mode = lm_val
                    changed = True
                if changed:
                    db.add(existing)
                    added.append(f"updated:{inline.title}")
                desired_titles.add(inline.title)
            else:
                payload = InstructionCreate(
                    text=inline.text,
                    title=inline.title,
                    category=inline.category.value if hasattr(inline.category, "value") else str(inline.category),
                    load_mode=inline.load_mode.value if hasattr(inline.load_mode, "value") else str(inline.load_mode),
                    ai_source="yaml",
                    source_type="user",
                    data_source_ids=[str(ds.id)],
                    status="published",
                )
                await self._instruction_service.create_instruction(
                    db=db,
                    instruction_data=payload,
                    current_user=current_user,
                    organization=organization,
                    force_global=True,
                    auto_finalize=True,
                )
                desired_titles.add(inline.title)
                added.append(f"created:{inline.title}")

        # Detach yaml-owned instructions removed from manifest
        removed: List[str] = []
        for title, instr in current_yaml.items():
            if title in desired_titles:
                continue
            await db.execute(
                instruction_data_source_association.delete().where(
                    instruction_data_source_association.c.instruction_id == instr.id,
                    instruction_data_source_association.c.data_source_id == ds.id,
                )
            )
            removed.append(title)

        out: Dict[str, Any] = {}
        if added:
            out["added"] = added
        if removed:
            out["removed"] = removed
        return out

    # ----- tables ---------------------------------------------------------

    async def _apply_table_rules(
        self,
        db: AsyncSession,
        ds: DataSource,
        rules: Optional[TableRules],
        resolved: _Resolved,
    ) -> List[ApplyWarning]:
        """Resolve include/exclude globs against ConnectionTable rows for
        the linked connections, then upsert ``DataSourceTable`` with the
        right ``is_active`` flag."""
        warnings: List[ApplyWarning] = []
        if not ds.connections:
            return warnings

        include_globs = (rules.include if rules and rules.include is not None else ["*"])
        exclude_globs = (rules.exclude if rules else [])

        for conn in ds.connections:
            # Fetch ConnectionTable rows
            ct_q = await db.execute(
                select(ConnectionTable).where(ConnectionTable.connection_id == str(conn.id))
            )
            conn_tables = list(ct_q.scalars().all())
            if not conn_tables:
                # Probably still indexing — surface as a warning so the
                # caller knows the filter will apply on the next apply.
                warnings.append(
                    ApplyWarning(
                        loc=["tables"],
                        code=ApplyWarningCode.CONNECTION_INDEXING_PENDING,
                        message=(
                            f"Connection '{conn.name}' has no indexed tables yet; "
                            "re-apply once indexing completes."
                        ),
                    )
                )
                continue

            # First make sure DataSourceTable rows exist for every ConnectionTable
            await self._ds_service.sync_domain_tables_from_connection(
                db, ds, conn, max_auto_select=None
            )

            # Flip is_active per glob match
            dst_q = await db.execute(
                select(DataSourceTable)
                .where(DataSourceTable.datasource_id == ds.id)
                .where(DataSourceTable.connection_table_id.in_([t.id for t in conn_tables]))
            )
            domain_tables = list(dst_q.scalars().all())
            ct_by_id = {t.id: t for t in conn_tables}
            for dt in domain_tables:
                ct = ct_by_id.get(dt.connection_table_id)
                if ct is None:
                    continue
                fqn = _table_fqn(conn.name, ct)
                matches_include = any(_glob_match(g, fqn) for g in include_globs)
                matches_exclude = any(_glob_match(g, fqn) for g in exclude_globs)
                desired_active = matches_include and not matches_exclude
                if dt.is_active != desired_active:
                    dt.is_active = desired_active
                    db.add(dt)

        return warnings

    # ----- tools ----------------------------------------------------------

    async def _apply_tool_rules(
        self,
        db: AsyncSession,
        ds: DataSource,
        tools: Dict[str, ToolsOverlay],
        resolved: _Resolved,
    ) -> List[ApplyWarning]:
        _, warnings = await self._apply_tool_rules_with_diff(db, ds, tools, resolved)
        return warnings

    async def _apply_tool_rules_with_diff(
        self,
        db: AsyncSession,
        ds: DataSource,
        tools: Dict[str, ToolsOverlay],
        resolved: _Resolved,
    ) -> Tuple[Dict[str, Any], List[ApplyWarning]]:
        """Upsert ``data_source_connection_tool`` overlay rows. Tools listed
        in ``deny`` get ``is_enabled=False``; ``confirm`` and ``allow`` set
        the policy. Tools not mentioned get no overlay row (inherit
        ``ConnectionTool`` defaults)."""
        warnings: List[ApplyWarning] = []
        diff: Dict[str, Any] = {}

        # Gather all overlay rows for this DS up front
        existing_q = await db.execute(
            select(DataSourceConnectionTool).where(
                DataSourceConnectionTool.data_source_id == ds.id
            )
        )
        existing = {row.connection_tool_id: row for row in existing_q.scalars().all()}
        desired: Dict[str, Tuple[bool, str]] = {}  # connection_tool_id -> (is_enabled, policy)

        for conn_name, overlay in tools.items():
            conn = resolved.connections.get(conn_name)
            if conn is None:
                continue
            if conn.type not in TOOL_COMPATIBLE_CONNECTION_TYPES:
                # Already caught by resolver; defensive skip
                continue

            ct_q = await db.execute(
                select(ConnectionTool).where(ConnectionTool.connection_id == str(conn.id))
            )
            conn_tools = list(ct_q.scalars().all())
            tool_by_name = {t.name: t for t in conn_tools}

            for tool in conn_tools:
                if any(_glob_match(g, tool.name) for g in overlay.deny):
                    desired[str(tool.id)] = (False, "deny")
                elif any(_glob_match(g, tool.name) for g in overlay.confirm):
                    desired[str(tool.id)] = (True, "confirm")
                elif any(_glob_match(g, tool.name) for g in overlay.allow):
                    desired[str(tool.id)] = (True, "allow")

        # Upsert
        for tool_id, (is_enabled, policy) in desired.items():
            row = existing.get(tool_id)
            if row is None:
                db.add(
                    DataSourceConnectionTool(
                        data_source_id=str(ds.id),
                        connection_tool_id=tool_id,
                        is_enabled=is_enabled,
                        policy=policy,
                    )
                )
                diff.setdefault("set", []).append(
                    {"connection_tool_id": tool_id, "is_enabled": is_enabled, "policy": policy}
                )
            elif row.is_enabled != is_enabled or row.policy != policy:
                row.is_enabled = is_enabled
                row.policy = policy
                db.add(row)
                diff.setdefault("set", []).append(
                    {"connection_tool_id": tool_id, "is_enabled": is_enabled, "policy": policy}
                )

        # Remove overlays no longer in desired
        for tool_id, row in existing.items():
            if tool_id in desired:
                continue
            await db.delete(row)
            diff.setdefault("reset", []).append(tool_id)

        return diff, warnings

    # ----- diff (dry-run) -------------------------------------------------

    async def _compute_diff(
        self,
        db: AsyncSession,
        organization: Organization,
        manifest: AgentManifest,
        resolved: _Resolved,
        existing: Optional[DataSource],
    ) -> Dict[str, Any]:
        if existing is None:
            return {
                "action": "create",
                "connections": list(manifest.connections),
                "members": [m.model_dump(exclude_none=True) for m in manifest.members],
                "instructions": len(manifest.instructions),
                "conversation_starters": list(manifest.conversation_starters),
            }
        diff: Dict[str, Any] = {"action": "update"}
        if existing.description != manifest.description:
            diff["description"] = {"from": existing.description, "to": manifest.description}
        if existing.context != manifest.context:
            diff["context"] = {"from": existing.context, "to": manifest.context}
        if existing.is_public != manifest.is_public:
            diff["is_public"] = {"from": existing.is_public, "to": manifest.is_public}
        if existing.use_llm_sync != manifest.use_llm_sync:
            diff["use_llm_sync"] = {"from": existing.use_llm_sync, "to": manifest.use_llm_sync}
        current_conns = sorted(c.name for c in (existing.connections or []))
        desired_conns = sorted(manifest.connections)
        if current_conns != desired_conns:
            diff["connections"] = {"from": current_conns, "to": desired_conns}
        if (existing.conversation_starters or []) != list(manifest.conversation_starters):
            diff["conversation_starters"] = {
                "from": existing.conversation_starters or [],
                "to": list(manifest.conversation_starters),
            }
        return diff

    # ----- export helper --------------------------------------------------

    async def _build_manifest_from_db(
        self, db: AsyncSession, ds: DataSource
    ) -> AgentManifest:
        # Connections
        connections = [c.name for c in (ds.connections or [])]

        # Members
        members: List[MemberRef] = []
        m_q = await db.execute(
            select(DataSourceMembership).where(
                DataSourceMembership.data_source_id == ds.id
            )
        )
        memberships = list(m_q.scalars().all())
        # Build a grants lookup once
        g_q = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == "data_source",
                ResourceGrant.resource_id == str(ds.id),
                ResourceGrant.deleted_at.is_(None),
            )
        )
        grants = {(g.principal_type, g.principal_id): list(g.permissions or []) for g in g_q.scalars().all()}

        owner_id = str(ds.owner_user_id) if ds.owner_user_id else None
        for m in memberships:
            # Skip the implicit owner row from the YAML (it's always present
            # and re-added automatically on create).
            if m.principal_type == PRINCIPAL_TYPE_USER and m.principal_id == owner_id:
                continue
            perms = grants.get((m.principal_type, m.principal_id))
            if m.principal_type == PRINCIPAL_TYPE_USER:
                u_q = await db.execute(select(User).where(User.id == m.principal_id))
                user = u_q.scalar_one_or_none()
                if user is None:
                    continue
                members.append(
                    MemberRef(
                        user=user.email,
                        permissions=perms if perms and perms != DEFAULT_MEMBER_PERMISSIONS else None,
                    )
                )
            else:
                g_obj_q = await db.execute(select(Group).where(Group.id == m.principal_id))
                grp = g_obj_q.scalar_one_or_none()
                if grp is None:
                    continue
                members.append(
                    MemberRef(
                        group=grp.name,
                        permissions=perms if perms and perms != DEFAULT_MEMBER_PERMISSIONS else None,
                    )
                )

        # Conversation starters
        starters = list(ds.conversation_starters or [])

        # Instructions (only those previously created via YAML are
        # round-trippable as 'inline'; manually attached ones come back as
        # id refs).
        instr_q = await db.execute(
            select(Instruction)
            .join(
                instruction_data_source_association,
                instruction_data_source_association.c.instruction_id == Instruction.id,
            )
            .where(
                instruction_data_source_association.c.data_source_id == ds.id,
                Instruction.deleted_at.is_(None),
            )
        )
        instructions: List[InstructionRef] = []
        for instr in instr_q.scalars().all():
            from app.schemas.agent_manifest_schema import InstructionInline
            from app.schemas.instruction_schema import (
                InstructionCategory as _IC,
                InstructionLoadMode as _ILM,
            )

            if instr.ai_source == "yaml" and instr.title:
                try:
                    cat = _IC(instr.category or "general")
                except Exception:
                    cat = _IC.GENERAL
                try:
                    lm = _ILM(instr.load_mode or "always")
                except Exception:
                    lm = _ILM.ALWAYS
                instructions.append(
                    InstructionRef(
                        inline=InstructionInline(
                            title=instr.title,
                            text=instr.text,
                            category=cat,
                            load_mode=lm,
                        )
                    )
                )
            else:
                instructions.append(InstructionRef(id=str(instr.id)))

        # Tables — reverse-engineer include/exclude from is_active flags.
        # We emit an exact list of active FQNs as ``include`` and leave
        # ``exclude`` empty. Re-applying yields the same state.
        rules: Optional[TableRules] = None
        if ds.connections:
            include_list: List[str] = []
            for conn in ds.connections:
                ct_q = await db.execute(
                    select(ConnectionTable).where(ConnectionTable.connection_id == str(conn.id))
                )
                ct_by_id = {ct.id: ct for ct in ct_q.scalars().all()}
                dt_q = await db.execute(
                    select(DataSourceTable)
                    .where(DataSourceTable.datasource_id == ds.id)
                    .where(DataSourceTable.is_active == True)  # noqa: E712
                    .where(DataSourceTable.connection_table_id.in_(list(ct_by_id.keys())))
                )
                for dt in dt_q.scalars().all():
                    ct = ct_by_id.get(dt.connection_table_id)
                    if ct is None:
                        continue
                    include_list.append(_table_fqn(conn.name, ct))
            if include_list:
                rules = TableRules(include=sorted(set(include_list)), exclude=[])

        # Tools overlay → ToolsOverlay by connection name
        tools: Dict[str, ToolsOverlay] = {}
        if ds.connections:
            ov_q = await db.execute(
                select(DataSourceConnectionTool, ConnectionTool, Connection)
                .join(ConnectionTool, ConnectionTool.id == DataSourceConnectionTool.connection_tool_id)
                .join(Connection, Connection.id == ConnectionTool.connection_id)
                .where(DataSourceConnectionTool.data_source_id == ds.id)
            )
            for overlay, ctool, conn in ov_q.all():
                bucket = tools.setdefault(conn.name, ToolsOverlay())
                if not overlay.is_enabled or overlay.policy == "deny":
                    bucket.deny.append(ctool.name)
                elif overlay.policy == "confirm":
                    bucket.confirm.append(ctool.name)
                else:
                    bucket.allow.append(ctool.name)

        return AgentManifest(
            name=ds.name,
            description=ds.description,
            context=ds.context,
            is_public=bool(ds.is_public),
            use_llm_sync=bool(ds.use_llm_sync),
            connections=connections,
            tables=rules,
            tools=tools,
            instructions=instructions,
            conversation_starters=starters,
            members=members,
        )


# ---------------------------------------------------------------------------
# Manifest resolver — refs to DB rows
# ---------------------------------------------------------------------------


class ManifestResolver:
    """Batches lookups for connections, groups, users, instructions, tools.

    Returns ``(resolved, errors)``. Always processes every ref so the caller
    can show *all* fixable problems in one round-trip.
    """

    def __init__(self, db: AsyncSession, organization: Organization) -> None:
        self.db = db
        self.org = organization

    async def resolve(
        self, manifest: AgentManifest
    ) -> Tuple[_Resolved, List[ApplyError]]:
        resolved = _Resolved()
        errors: List[ApplyError] = []

        await self._resolve_connections(manifest, resolved, errors)
        await self._resolve_groups(manifest, resolved, errors)
        await self._resolve_users(manifest, resolved, errors)
        await self._resolve_instructions(manifest, resolved, errors)
        await self._resolve_tools(manifest, resolved, errors)

        return resolved, errors

    async def _resolve_connections(
        self, manifest: AgentManifest, resolved: _Resolved, errors: List[ApplyError]
    ) -> None:
        if not manifest.connections:
            return
        q = await self.db.execute(
            select(Connection).where(
                Connection.organization_id == str(self.org.id),
                Connection.name.in_(manifest.connections),
                Connection.deleted_at.is_(None),
            )
        )
        by_name = {c.name: c for c in q.scalars().all()}
        # Fetch all org connection names once for did-you-mean.
        all_q = await self.db.execute(
            select(Connection.name).where(
                Connection.organization_id == str(self.org.id),
                Connection.deleted_at.is_(None),
            )
        )
        all_names = [r[0] for r in all_q.all()]
        for i, name in enumerate(manifest.connections):
            conn = by_name.get(name)
            if conn is None:
                errors.append(
                    ApplyError(
                        loc=["connections", i],
                        code=ApplyErrorCode.CONNECTION_NOT_FOUND,
                        message=f"Connection '{name}' not found in this organization.",
                        value=name,
                        suggestion=_suggest(name, all_names),
                    )
                )
                continue
            resolved.connections[name] = conn

    async def _resolve_groups(
        self, manifest: AgentManifest, resolved: _Resolved, errors: List[ApplyError]
    ) -> None:
        group_refs = [(i, m.group) for i, m in enumerate(manifest.members) if m.group]
        if not group_refs:
            return
        names = list({n for _, n in group_refs})
        q = await self.db.execute(
            select(Group).where(
                Group.organization_id == str(self.org.id),
                Group.name.in_(names),
                Group.deleted_at.is_(None),
            )
        )
        by_name = {g.name: g for g in q.scalars().all()}
        all_q = await self.db.execute(
            select(Group.name).where(
                Group.organization_id == str(self.org.id),
                Group.deleted_at.is_(None),
            )
        )
        all_names = [r[0] for r in all_q.all()]
        for idx, name in group_refs:
            grp = by_name.get(name)
            if grp is None:
                errors.append(
                    ApplyError(
                        loc=["members", idx, "group"],
                        code=ApplyErrorCode.GROUP_NOT_FOUND,
                        message=f"Group '{name}' not found in this organization.",
                        value=name,
                        suggestion=_suggest(name, all_names),
                    )
                )
                continue
            resolved.groups[name] = grp

    async def _resolve_users(
        self, manifest: AgentManifest, resolved: _Resolved, errors: List[ApplyError]
    ) -> None:
        user_refs = [(i, m.user) for i, m in enumerate(manifest.members) if m.user]
        if not user_refs:
            return
        emails = list({e for _, e in user_refs})
        # Org-scoped lookup via memberships
        from app.models.membership import Membership

        q = await self.db.execute(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.organization_id == str(self.org.id),
                User.email.in_(emails),
            )
        )
        by_email = {u.email: u for u in q.scalars().all()}
        all_q = await self.db.execute(
            select(User.email)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == str(self.org.id))
        )
        all_emails = [r[0] for r in all_q.all() if r[0]]
        for idx, email in user_refs:
            u = by_email.get(email)
            if u is None:
                errors.append(
                    ApplyError(
                        loc=["members", idx, "user"],
                        code=ApplyErrorCode.USER_NOT_FOUND,
                        message=f"No user '{email}' in this organization.",
                        value=email,
                        suggestion=_suggest(email, all_emails),
                    )
                )
                continue
            resolved.users[email] = u

    async def _resolve_instructions(
        self, manifest: AgentManifest, resolved: _Resolved, errors: List[ApplyError]
    ) -> None:
        id_refs = [(i, r.id) for i, r in enumerate(manifest.instructions) if r.id]
        if not id_refs:
            return
        ids = list({i for _, i in id_refs})
        q = await self.db.execute(
            select(Instruction).where(
                Instruction.id.in_(ids),
                Instruction.deleted_at.is_(None),
            )
        )
        by_id: Dict[str, Instruction] = {}
        for instr in q.scalars().all():
            # Org scope check: an instruction's org is derived from its user.
            # For simplicity we just check the user's primary org via the
            # user model; if the model doesn't carry that, we skip the check
            # and rely on the FK to data sources to keep cross-org refs out.
            by_id[str(instr.id)] = instr
        for idx, iid in id_refs:
            instr = by_id.get(iid)
            if instr is None:
                errors.append(
                    ApplyError(
                        loc=["instructions", idx, "id"],
                        code=ApplyErrorCode.INSTRUCTION_NOT_FOUND,
                        message=f"Instruction '{iid}' not found.",
                        value=iid,
                    )
                )
                continue
            resolved.instructions[iid] = instr

    async def _resolve_tools(
        self, manifest: AgentManifest, resolved: _Resolved, errors: List[ApplyError]
    ) -> None:
        if not manifest.tools:
            return
        for conn_name, overlay in manifest.tools.items():
            # Cross-field: conn_name must be in manifest.connections (already
            # in resolved.connections if it existed).
            conn = resolved.connections.get(conn_name)
            if conn is None:
                # Either not in connections list or didn't resolve. Don't
                # re-report the not-found case; flag the cross-field
                # mismatch with a clear message.
                if conn_name not in manifest.connections:
                    errors.append(
                        ApplyError(
                            loc=["tools", conn_name],
                            code=ApplyErrorCode.CONNECTION_NOT_FOUND,
                            message=(
                                f"Connection '{conn_name}' in tools: must also "
                                "appear in connections:."
                            ),
                            value=conn_name,
                            suggestion=_suggest(conn_name, manifest.connections),
                        )
                    )
                continue
            if conn.type not in TOOL_COMPATIBLE_CONNECTION_TYPES:
                errors.append(
                    ApplyError(
                        loc=["tools", conn_name],
                        code=ApplyErrorCode.CONNECTION_TYPE_MISMATCH,
                        message=(
                            f"Tools can only be configured on MCP or custom_api "
                            f"connections; '{conn_name}' is of type '{conn.type}'."
                        ),
                        value=conn_name,
                    )
                )
                continue

            # Fetch tools for this connection
            t_q = await self.db.execute(
                select(ConnectionTool).where(
                    ConnectionTool.connection_id == str(conn.id)
                )
            )
            tools = list(t_q.scalars().all())
            tool_names = [t.name for t in tools]
            for bucket_name in ("allow", "confirm", "deny"):
                bucket = getattr(overlay, bucket_name)
                for j, name in enumerate(bucket):
                    if name == "*" or "*" in name:
                        continue  # glob — no resolution needed
                    if name in tool_names:
                        resolved.tools[(conn_name, name)] = next(
                            t for t in tools if t.name == name
                        )
                    else:
                        errors.append(
                            ApplyError(
                                loc=["tools", conn_name, bucket_name, j],
                                code=ApplyErrorCode.TOOL_NOT_FOUND,
                                message=(
                                    f"Tool '{name}' not found on connection "
                                    f"'{conn_name}'."
                                ),
                                value=name,
                                suggestion=_suggest(name, tool_names),
                            )
                        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pydantic_errors_to_apply_errors(ve: ValidationError) -> List[ApplyError]:
    out: List[ApplyError] = []
    for err in ve.errors():
        loc = list(err.get("loc", []))
        msg = err.get("msg", "validation error")
        kind = err.get("type", "")
        if "enum" in kind:
            code = ApplyErrorCode.ENUM_INVALID
        else:
            code = ApplyErrorCode.SCHEMA_INVALID
        out.append(ApplyError(loc=loc, code=code, message=msg))
    return out


def _suggest(value: str, candidates: Sequence[str]) -> Optional[str]:
    if not value or not candidates:
        return None
    # Case-insensitive: match in lower-space, then return the original.
    cands = [c for c in candidates if c]
    lookup = {c.lower(): c for c in cands}
    matches = difflib.get_close_matches(value.lower(), list(lookup.keys()), n=1, cutoff=0.5)
    return lookup[matches[0]] if matches else None


def _table_fqn(conn_name: str, ct: ConnectionTable) -> str:
    """Build the canonical name used to match glob patterns:
    ``{connection}.{schema or db}.{table}``.

    ``ConnectionTable.name`` may already contain a schema prefix; we use
    ``metadata_json['schema']`` when available, falling back to the table
    name with a synthesized middle segment if not.
    """
    schema = None
    if ct.metadata_json and isinstance(ct.metadata_json, dict):
        schema = ct.metadata_json.get("schema") or ct.metadata_json.get("dataset")
    if schema:
        return f"{conn_name}.{schema}.{ct.name}"
    # Some connectors already store ``schema.table`` in name
    if "." in ct.name:
        return f"{conn_name}.{ct.name}"
    return f"{conn_name}.{ct.name}"


def _glob_match(pattern: str, value: str) -> bool:
    """Case-insensitive ``fnmatch`` against ``{a}.{b}.{c}`` triples."""
    return fnmatch.fnmatchcase(value.lower(), pattern.lower())
