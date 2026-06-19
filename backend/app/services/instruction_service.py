from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload, lazyload
from sqlalchemy import and_, or_
from typing import List, Optional, Any, Dict
from fastapi import HTTPException

from app.models.instruction import (
    Instruction,
    instruction_data_source_association,
)

from app.ai.agents.suggest_instructions.suggest_instructions import SuggestInstructions
from app.models.data_source import DataSource
from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
from app.models.metadata_resource import MetadataResource
from app.models.datasource_table import DataSourceTable
from app.models.user import User
from app.models.organization import Organization
from app.models.instruction_label import InstructionLabel
from app.schemas.instruction_schema import (
    InstructionCreate, 
    InstructionUpdate, 
    InstructionSchema,
    InstructionListSchema,
)
from app.schemas.user_schema import UserSchema
from app.schemas.instruction_analysis_schema import (
    InstructionAnalysisRequest,
    InstructionAnalysisResponse,
    ImpactEstimation,
    RelatedInstructionItem,
    RelatedInstructions,
    RelatedResourceItem,
    RelatedResources,
)

from app.schemas.instruction_reference_schema import InstructionReferenceSchema
from app.services.instruction_reference_service import InstructionReferenceService
from app.services.llm_service import LLMService
from app.services.build_service import BuildService
from app.services.instruction_version_service import InstructionVersionService
from app.services.organization_settings_service import OrganizationSettingsService
from app.dependencies import async_session_maker
from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
from app.core.telemetry import telemetry
from app.ee.audit.service import audit_service
from app.models.completion import Completion
from app.models.report import Report
from sqlalchemy import select, func, or_, and_
import re
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class InstructionService:
    def __init__(self):
        self.reference_service = InstructionReferenceService()
        self.llm_service = LLMService()
        self.build_service = BuildService()
        self.version_service = InstructionVersionService()
    
    async def create_instruction(
        self,
        db: AsyncSession,
        instruction_data: InstructionCreate,
        current_user: User,
        organization: Organization,
        force_global: bool = False,
        build = None,  # Optional: use existing build instead of creating new one
        auto_finalize: bool = True,  # If False, skip auto-finalization (for batching)
        agent_execution_id: str = None,  # Optional: link instruction to agent execution (for training mode)
        version_status_override: Optional[str] = None,  # AI flows pass 'published' to flip the live row on build promotion
    ) -> InstructionSchema:
        """Create a new instruction. Approval workflow is handled by builds, not instruction status."""
        
        # Get user permissions for auto-publish check
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        
        # Validate data sources if provided
        if instruction_data.data_source_ids:
            await self._validate_data_sources(db, instruction_data.data_source_ids, organization)

        # Validate labels if provided
        if getattr(instruction_data, "label_ids", None):
            await self._validate_labels(db, instruction_data.label_ids, organization)

        # Convert enum strings coming from the API and extract their values
        raw = instruction_data.model_dump(exclude={'data_source_ids', 'references', 'label_ids'})
        instruction = Instruction(**raw)
        instruction.user_id = current_user.id
        instruction.organization_id = organization.id
        if agent_execution_id:
            instruction.agent_execution_id = agent_execution_id
        
        # SIMPLIFIED: All instructions are "published" (content ready)
        # Approval workflow is handled by builds, not instruction status
        # - Non-admin: build stays in pending_approval for admin review
        # - Admin: build auto-approved and promoted to main
        instruction.status = instruction_data.status or "published"
        # Leave private_status and global_status as NULL (deprecated)

        # Skills always use 'intelligent' (smart) retrieval — enforce server-side
        # so the load_mode can never drift to 'always'/'disabled' for a skill.
        self._enforce_skill_load_mode(instruction)

        db.add(instruction)
        await db.commit()
        # Refresh ID + any relationships that will be set below
        refresh_attrs = ['id']
        if instruction_data.data_source_ids:
            refresh_attrs.append('data_sources')
        if getattr(instruction_data, "label_ids", None):
            refresh_attrs.append('labels')
        await db.refresh(instruction, refresh_attrs)

        # Associate with data sources if provided
        if instruction_data.data_source_ids:
            await self._associate_data_sources(db, instruction, instruction_data.data_source_ids)

        # Associate with labels if provided
        if getattr(instruction_data, "label_ids", None):
            await self._associate_labels(db, instruction, instruction_data.label_ids)

        # Handle references if provided
        if getattr(instruction_data, "references", None) is not None:
            # Pass data source IDs for validation (empty list means all data sources)
            ds_ids = instruction_data.data_source_ids if instruction_data.data_source_ids else None
            await self.reference_service.replace_for_instruction(db, instruction.id, instruction_data.references or [], organization, ds_ids)
            await db.commit()  # Commit the references
        
        # === Build System Integration ===
        # Create version and add to build for ALL instructions (including draft/suggested)
        #
        # Capture the instruction id as a plain string up front. If a build step
        # fails and we roll back, the ORM objects become expired — touching any
        # attribute (even for a log line) would trigger an implicit lazy load on
        # a dead transaction and blow up with MissingGreenlet. Plain strings are
        # safe to use after a rollback.
        instr_id = str(instruction.id)
        try:
            # Re-fetch instruction with relationships for version creation
            await db.refresh(instruction, ['data_sources', 'labels', 'references'])

            # Create the first version
            version = await self.version_service.create_version(
                db, instruction, user_id=current_user.id,
                status_override=version_status_override,
            )

            # Update instruction's current version
            instruction.current_version_id = version.id

            # Use provided build or get/create a draft build for user changes
            target_build = build
            if target_build is None:
                target_build = await self.build_service.get_or_create_draft_build(
                    db, organization.id, source='user', user_id=current_user.id
                )
            version_id = str(version.id)
            target_build_id = str(target_build.id)

            # Add the version to the build
            await self.build_service.add_to_build(
                db, target_build_id, instr_id, version_id
            )

            await db.commit()

            # Auto-finalize unless explicitly disabled (for batching scenarios).
            # When disabled (agent batching), the session-end flow finalizes later,
            # so there's nothing to fail here.
            if auto_finalize:
                finalized = await self._auto_finalize_build(db, target_build, current_user, user_permissions)
            else:
                finalized = True

            logger.info(f"Created version {version_id} for instruction {instr_id}, added to build {target_build_id}")
        except Exception as e:
            logger.warning(f"Failed to create version for instruction {instr_id}: {e}")
            # Roll back so the session is clean for the cleanup/return below.
            try:
                await db.rollback()
            except Exception:
                pass
            finalized = False

        # If a synchronous create couldn't be made live — e.g. a concurrent
        # long-running transaction (the agent's shared session, or a leaked one)
        # held the shared ``instruction_builds`` lock until ``lock_timeout``
        # fired — don't leave a half-created instruction stranded in a build
        # that will never reach main (it would be invisible in the list, which
        # is the reported "instruction was not created" symptom). Soft-delete it
        # and signal a fast, retryable error instead of hanging or 500-ing.
        if auto_finalize and not finalized:
            try:
                orphan = await db.get(Instruction, instr_id)
                if orphan is not None and orphan.deleted_at is None:
                    orphan.deleted_at = datetime.utcnow()
                    await db.commit()
            except Exception:
                try:
                    await db.rollback()
                except Exception:
                    pass
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not save the instruction because the instruction store "
                    "is busy with another update. Please try again."
                ),
            )

        # Re-fetch instruction fresh with eager loading. This is also our
        # recovery point if a (non-fatal) build failure rolled the session back
        # above: re-reading by id rebinds clean ORM state so the lines below
        # (telemetry/audit/return) never touch an expired object on a dead
        # transaction.
        fresh_instruction = await db.execute(
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).options(
                    selectinload(DataSource.data_source_memberships),
                    selectinload(DataSource.primary_instruction),
                ),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
                selectinload(Instruction.labels),
            )
            .where(Instruction.id == instr_id)
        )
        instruction = fresh_instruction.scalar_one()

        # Telemetry: emit minimal, non-PII metadata using existing fields only
        try:
            refs = getattr(instruction_data, "references", None) or []
            await telemetry.capture(
                "instruction_created",
                {
                    "instruction_id": str(instruction.id),
                    "status": instruction.status,
                    "category": getattr(instruction, "category", None),
                    "is_seen": bool(getattr(instruction, "is_seen", False)),
                    "text_words_length": len((instruction.text.split() or [])),
                    "num_data_sources": len(instruction_data.data_source_ids or []),
                    "num_references_total": len(refs),
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            # Never fail the request due to telemetry
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="instruction.created",
                user_id=str(current_user.id),
                resource_type="instruction",
                resource_id=str(instruction.id),
                details={"title": instruction.title, "category": instruction.category},
            )
        except Exception:
            pass

        return await self._instruction_to_schema_with_references(db, instruction)
    
    async def analyze_instruction(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        request: InstructionAnalysisRequest,
    ) -> InstructionAnalysisResponse:
        """Naive analysis for instruction text with no external dependencies."""
        include = set(request.include or ["impact", "related_instructions"])
        meta: dict = {}
        started_at = datetime.utcnow()

        # 1) Tokenize instruction text (very naive)
        tokens = self._tokenize_text(request.text)
        if not tokens:
            impact = ImpactEstimation(score=0.0, prompts=[])
            result = InstructionAnalysisResponse(impact=impact if "impact" in include else None, meta={"took_ms": 0})
            return result

        impact_result: ImpactEstimation | None = None
        related_instructions_result: RelatedInstructions | None = None
        related_resources_result: RelatedResources | None = None

        # 2) Impact estimation via completions prompts
        if "impact" in include:
            impact_result = await self._compute_naive_impact(db, organization, tokens, request)

        # 3) Related instructions (permissions respected via existing query)
        if "related_instructions" in include:
            rel_insts_response = await self.get_instructions(
                db=db,
                organization=organization,
                current_user=current_user,
                skip=0,
                limit=500,
                include_own=True,
                include_drafts=False,
                include_archived=False,
                include_hidden=False,
            )
            # Extract items from paginated response
            rel_insts = rel_insts_response.get("items", [])
            # Naive token filter similar to prompts matching
            def text_matches(s: Optional[str]) -> bool:
                text_l = (s or "").lower()
                for t in tokens:
                    if t in text_l:
                        return True
                return False
            filtered = []
            since_dt = None
            if request.created_since_days and request.created_since_days > 0:
                since_dt = datetime.utcnow() - timedelta(days=request.created_since_days)
            for it in rel_insts:
                if since_dt and getattr(it, "created_at", None) and it.created_at and it.created_at < since_dt:
                    continue
                if text_matches(it.text):
                    filtered.append(it)
            # Exclude the same instruction if analyzing an existing one
            exclude_id = (request.instruction_id or "").strip()
            # Do NOT fallback to the full list when no matches; return empty when nothing is relevant
            base_candidates = filtered
            candidates = [it for it in base_candidates if not exclude_id or str(it.id) != exclude_id]
            ranked = self._rank_related_instructions(tokens, candidates)
            top_k = ranked[: max(0, request.limits.instructions)]
            items = [
                RelatedInstructionItem(
                    id=i.id,
                    text=i.text,
                    status=i.status,
                    createdByName=(getattr(i.user, "name", None) or getattr(i.user, "email", None) or None) if getattr(i, "user", None) else None,
                )
                for i in top_k
            ]
            related_instructions_result = RelatedInstructions(count=len(ranked), items=items, tokens=list(tokens))

        # 4) Related metadata resources (very naive name contains)
        if "resources" in include:
            # Use simple LIKE matching against MetadataResource name/path for current org
            query = (
                select(MetadataResource)
                .join(DataSource, MetadataResource.data_source_id == DataSource.id)
                .where(DataSource.organization_id == organization.id)
            )
            # Build OR of name/path ILIKE for tokens
            like_clauses = []
            for t in tokens:
                like = f"%{t}%"
                like_clauses.append(MetadataResource.name.ilike(like))
                like_clauses.append(MetadataResource.path.ilike(like))
            if like_clauses:
                query = query.where(or_(*like_clauses))
            query = query.limit(max(0, request.limits.resources))
            result = await db.execute(query)
            resources = result.scalars().all()
            items = [
                RelatedResourceItem(
                    id=str(r.id),
                    name=r.name,
                    resource_type=r.resource_type,
                    path=r.path,
                    description=getattr(r, "description", None),
                    sql_content=getattr(r, "sql_content", None),
                    raw_data=getattr(r, "raw_data", None),
                    columns=getattr(r, "columns", None),
                    depends_on=getattr(r, "depends_on", None),
                )
                for r in resources
            ]
            related_resources_result = RelatedResources(count=len(items), items=items)

        took_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        meta["took_ms"] = took_ms

        return InstructionAnalysisResponse(
            impact=impact_result,
            related_instructions=related_instructions_result,
            resources=related_resources_result,
            meta=meta,
        )

    async def _compute_naive_impact(
        self,
        db: AsyncSession,
        organization: Organization,
        tokens: set[str],
        request: InstructionAnalysisRequest,
    ) -> ImpactEstimation:
        """Compute naive impact: matched_prompts / total_prompts using simple substring checks in Python."""
        # Build base query: completions in this org with prompt.content present
        base_q = (
            select(Completion)
            .join(Report, Completion.report_id == Report.id)
            .where(
                and_(
                    Report.organization_id == organization.id,
                )
            )
            .order_by(Completion.created_at.desc())
            .limit(max(100, min(10000, request.max_prompts_scan)))
        )
        # Date filter
        if request.created_since_days and request.created_since_days > 0:
            since = datetime.utcnow() - timedelta(days=request.created_since_days)
            base_q = base_q.where(Completion.created_at >= since)

        result = await db.execute(base_q)
        completions = result.scalars().all()

        # Count prompts and matches
        total_prompts = 0
        matched_prompts = 0
        sample_prompts: list[dict] = []
        token_list = list(tokens)

        def content_matches(text: str) -> bool:
            text_l = (text or "").lower()
            for t in token_list:
                if t in text_l:
                    return True
            return False

        for c in completions:
            try:
                prompt_obj = c.prompt or {}
                content = ""
                if isinstance(prompt_obj, dict):
                    content = prompt_obj.get("content") or ""
                elif isinstance(prompt_obj, str):
                    content = prompt_obj
                else:
                    content = ""
                if content:
                    total_prompts += 1
                    if content_matches(content):
                        matched_prompts += 1
                        if len(sample_prompts) < max(0, request.limits.prompts):
                            sample_prompts.append({"content": content, "created_at": getattr(c, "created_at", None)})
            except Exception:
                # Ignore malformed prompts
                continue

        score = 0.0 if total_prompts == 0 else min(1.0, matched_prompts / total_prompts)
        return ImpactEstimation(
            score=round(score, 4),
            prompts=sample_prompts,
            matched_count=matched_prompts,
            total_count=total_prompts,
        )

    def _tokenize_text(self, text: str) -> set[str]:
        """Very naive tokenizer; lowercase, split on non-alphanum, drop short and common stopwords."""
        s = (text or "").lower()
        raw = re.split(r"[^a-z0-9_.]+", s)
        stop = {"the", "a", "an", "of", "and", "for", "to", "in", "by", "with", "on", "is", "are", "be", "this", "that"}
        tokens = {t for t in raw if t and len(t) >= 3 and t not in stop}
        return tokens

    def _rank_related_instructions(self, tokens: set[str], items: List[InstructionListSchema]) -> List[InstructionListSchema]:
        """Rank by naive Jaccard similarity on tokens within text."""
        def jaccard(a: set[str], b: set[str]) -> float:
            u = len(a | b)
            return 0.0 if u == 0 else len(a & b) / u
        scored: list[tuple[float, InstructionListSchema]] = []
        for it in items:
            t = self._tokenize_text(it.text or "")
            scored.append((jaccard(tokens, t), it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored]

    async def get_instructions(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        skip: int = 0,
        limit: int = 50,
        status: Optional[str] = None,
        categories: Optional[List[str]] = None,
        include_own: bool = True,
        include_drafts: bool = False,
        include_archived: bool = False,
        include_hidden: bool = False,
        user_id: Optional[str] = None,
        data_source_ids: Optional[List[str]] = None,
        source_types: Optional[List[str]] = None,
        load_modes: Optional[List[str]] = None,
        label_ids: Optional[List[str]] = None,
        search: Optional[str] = None,
        build_id: Optional[str] = None,
        include_global: bool = True
    ) -> dict:
        """Get instructions with clean permission-based filtering. Returns paginated response.
        
        By default, loads instructions from the main build (is_main=True).
        Pass build_id to load from a specific build instead.
        """
        
        user_permissions = await self._get_user_permissions(db, current_user, organization)

        # Build the query conditions cleanly
        conditions = []
        
        # Add user's own instructions
        if include_own:
            conditions.append(self._get_own_instructions_condition(current_user.id))
        
        # Add others' instructions based on permissions
        others_condition = self._get_others_instructions_condition(
            current_user.id, 
            user_permissions, 
            include_drafts, 
            include_archived, 
            include_hidden
        )
        if others_condition is not None:
            conditions.append(others_condition)
        
        # Handle admin user filtering
        if user_id and self._can_filter_by_user(user_permissions):
            conditions = [Instruction.user_id == user_id]
        
        # Execute query with new filters
        return await self._execute_instructions_query(
            db, organization, conditions, status, categories, skip, limit,
            data_source_ids, source_types, load_modes, label_ids, search,
            build_id=build_id, include_global=include_global,
            current_user=current_user,
        )

    async def get_available_source_types(
        self,
        db: AsyncSession,
        organization: Organization
    ) -> List[Dict[str, Any]]:
        """Get available source types based on existing instructions.
        
        Returns a list of source type objects with value, label, and icon info.
        - User: only shown if user instructions exist
        - AI: always shown
        - Git: always shown (covers all git-sourced instructions)
        - Plus dynamic sub-types (dbt, markdown, etc.) if they exist
        """
        from sqlalchemy import distinct
        
        available_types = []
        
        # Check for user instructions (only show if exists)
        user_count = await db.scalar(
            select(func.count(distinct(Instruction.id)))
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None,
                    Instruction.source_type == 'user'
                )
            )
        )
        if user_count and user_count > 0:
            available_types.append({
                'value': 'user',
                'label': 'User',
                'heroicon': 'i-heroicons-user'
            })
        
        # AI is always shown
        available_types.append({
            'value': 'ai',
            'label': 'AI',
            'heroicon': 'i-heroicons-sparkles'
        })
        
        # Git is always shown (covers all git-sourced instructions)
        available_types.append({
            'value': 'git',
            'label': 'Git',
            'icon': '/icons/git-branch.svg'
        })
        
        # Check for dbt instructions (git + dbt_* resource types)
        # Support both: new flow (structured_data->>'resource_type') and legacy (MetadataResource join)
        dbt_count = await db.scalar(
            select(func.count(distinct(Instruction.id)))
            .select_from(Instruction)
            .outerjoin(MetadataResource, Instruction.source_metadata_resource_id == MetadataResource.id)
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None,
                    Instruction.source_type == 'git',
                    or_(
                        Instruction.structured_data['resource_type'].as_string().like('dbt_%'),
                        MetadataResource.resource_type.like('dbt_%'),
                    )
                )
            )
        )
        if dbt_count and dbt_count > 0:
            available_types.append({
                'value': 'dbt',
                'label': 'dbt',
                'icon': '/icons/dbt.png'
            })

        # Check for markdown instructions
        markdown_count = await db.scalar(
            select(func.count(distinct(Instruction.id)))
            .select_from(Instruction)
            .outerjoin(MetadataResource, Instruction.source_metadata_resource_id == MetadataResource.id)
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None,
                    Instruction.source_type == 'git',
                    or_(
                        Instruction.structured_data['resource_type'].as_string() == 'markdown_document',
                        MetadataResource.resource_type == 'markdown_document',
                    )
                )
            )
        )
        if markdown_count and markdown_count > 0:
            available_types.append({
                'value': 'markdown',
                'label': 'Markdown',
                'icon': '/icons/markdown.png'
            })
        
        return available_types

    async def get_instruction(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        organization: Organization,
        current_user: User
    ) -> Optional[InstructionSchema]:
        """Get a single instruction by ID"""

        # Singular response uses InstructionSchema → DataSourceSchema, which
        # includes memberships, git_repository, and connections. Suppress the
        # rest of the DS lazy="selectin" cascade (reports/widgets/queries/...)
        # which the response never exposes.
        query = (
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).options(
                    lazyload("*"),
                    selectinload(DataSource.data_source_memberships),
                    selectinload(DataSource.git_repository),
                    selectinload(DataSource.connections).options(lazyload("*")),
                    selectinload(DataSource.primary_instruction),
                ),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
                selectinload(Instruction.labels),
            )
            .where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        
        result = await db.execute(query)
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            return None

        return await self._instruction_to_schema_with_references(db, instruction)

    async def user_can_view_instruction(
        self,
        db: AsyncSession,
        instruction,
        current_user: User,
        organization: Organization,
    ) -> bool:
        """Mirror the list's per-data-source visibility for a single instruction.

        Viewable iff the instruction is global (attached to no data source) or
        attached to a data source the user is an explicit member of, or that is
        public. Used to gate read/detail endpoints (the instruction modal,
        version history, pending builds) so an instruction for an agent the user
        never joined isn't reachable by id even though it's hidden from the list.

        NOTE: this gates *viewing* only. Management endpoints (update/delete/
        revert) keep their own resource-permission checks, where org admins
        retain capability bypass.
        """
        ds_list = getattr(instruction, "data_sources", None) or []
        ds_ids = {str(getattr(d, "id", None)) for d in ds_list if getattr(d, "id", None)}
        if not ds_ids:
            return True  # global instruction — visible to everyone

        from app.core.permission_resolver import get_member_data_source_ids
        member_ids = {
            str(m)
            for m in await get_member_data_source_ids(
                db, str(current_user.id), str(organization.id)
            )
        }
        if ds_ids & member_ids:
            return True

        result = await db.execute(
            select(DataSource.id).where(
                and_(
                    DataSource.id.in_(ds_ids),
                    DataSource.organization_id == organization.id,
                    DataSource.is_public == True,
                )
            )
        )
        public_ids = {str(r[0]) for r in result.all()}
        return bool(ds_ids & public_ids)

    async def update_instruction(
        self,
        db: AsyncSession,
        instruction_id: str,
        instruction_data: InstructionUpdate,
        organization: Organization,
        current_user: User
    ) -> Optional[InstructionSchema]:
        """Update an instruction with proper permission and workflow handling"""
        
        # Get the instruction
        instruction = await self._get_instruction_by_id(db, instruction_id, organization)
        # Determine membership/permissions; non-members cannot update regardless of ownership
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        if not user_permissions:
            raise HTTPException(status_code=403, detail="Permission denied: not an organization member")
        
        # Determine what type of update this is and check permissions
        update_type = self._determine_update_type(instruction, instruction_data, current_user, user_permissions)
        
        # Handle the update based on type
        if update_type == "admin_edit":
            await self._handle_admin_edit(instruction, instruction_data, current_user)
        elif update_type == "owner_edit":
            await self._handle_owner_edit(instruction, instruction_data)
        elif update_type == "suggester_edit":
            # Suggester edits use the same safe field subset as owner edits (no status changes).
            # The downstream build-auto-finalize logic will route the resulting build to
            # pending_approval since the user is not an admin.
            await self._handle_owner_edit(instruction, instruction_data)
        else:
            raise HTTPException(status_code=403, detail="Permission denied")

        # Skills always use 'intelligent' (smart) retrieval. Enforce after the
        # edit handlers run so a kind→skill change (or a skill whose load_mode
        # was passed as 'always'/'disabled') is normalized before versioning.
        self._enforce_skill_load_mode(instruction)

        # Handle data source associations
        if instruction_data.data_source_ids is not None:
            if instruction_data.data_source_ids:
                await self._validate_data_sources(db, instruction_data.data_source_ids, organization)
            await self._update_data_source_associations(db, instruction, instruction_data.data_source_ids)

        # Handle label associations
        if getattr(instruction_data, "label_ids", None) is not None:
            if instruction_data.label_ids:
                await self._validate_labels(db, instruction_data.label_ids, organization)
            await self._update_label_associations(db, instruction, instruction_data.label_ids)
        
        # Handle references if provided
        if getattr(instruction_data, "references", None) is not None:
            # Get current data source IDs for the instruction if not provided in update
            ds_ids = instruction_data.data_source_ids
            if ds_ids is None:
                # Get current data source associations
                current_ds_ids = [ds.id for ds in instruction.data_sources] if instruction.data_sources else None
            else:
                current_ds_ids = ds_ids if ds_ids else None
            await self.reference_service.replace_for_instruction(db, instruction.id, instruction_data.references or [], organization, current_ds_ids)

        await db.commit()
        
        # === Build System Integration ===
        # Create new version if content has changed
        try:
            # Re-fetch instruction with relationships for version creation
            fresh_for_version = await db.execute(
                select(Instruction)
                .options(
                    selectinload(Instruction.data_sources),
                    selectinload(Instruction.labels),
                    selectinload(Instruction.references),
                )
                .where(Instruction.id == instruction.id)
            )
            instruction_with_rels = fresh_for_version.scalar_one()
            
            # Check if content has changed
            if await self.version_service.has_content_changed(db, instruction_with_rels):
                # Create new version
                version = await self.version_service.create_version(
                    db, instruction_with_rels, user_id=current_user.id
                )
                
                # Update instruction's current version
                instruction_with_rels.current_version_id = version.id
                
                # Check if we're targeting an existing build (editing within BuildExplorerModal)
                target_build_id = getattr(instruction_data, 'target_build_id', None)
                
                if target_build_id:
                    # Use the specified target build - don't create new one or auto-finalize
                    target_build = await self.build_service.get_build(db, target_build_id)
                    if target_build and target_build.can_be_edited:
                        await self.build_service.add_to_build(
                            db, target_build.id, instruction_with_rels.id, version.id
                        )
                        await db.commit()
                        logger.debug(f"Created version {version.id} for instruction {instruction.id}, added to existing build {target_build.id}")
                    else:
                        logger.warning(f"Target build {target_build_id} not found or not editable, skipping build update")
                else:
                    # Default behavior: Get or create a draft build for user changes
                    build = await self.build_service.get_or_create_draft_build(
                        db, organization.id, source='user', user_id=current_user.id
                    )
                    
                    # Add the version to the build
                    await self.build_service.add_to_build(
                        db, build.id, instruction_with_rels.id, version.id
                    )
                    
                    await db.commit()
                    
                    # Always auto-finalize to keep main build in sync
                    # All instructions (including draft/suggested) are in main for UI display
                    await self._auto_finalize_build(db, build, current_user, user_permissions)
                    
                    logger.debug(f"Created version {version.id} for instruction {instruction.id}, added to build {build.id}")
        except Exception as e:
            logger.warning(f"Failed to create version for updated instruction {instruction.id}: {e}")
            # Don't fail the update if versioning fails
        
        # Re-fetch instruction with proper eager loading to avoid lazy loading issues
        fresh_instruction = await db.execute(
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).options(
                    selectinload(DataSource.data_source_memberships),
                    selectinload(DataSource.primary_instruction),
                ),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
                selectinload(Instruction.labels),
            )
            .where(Instruction.id == instruction.id)
        )
        instruction = fresh_instruction.scalar_one()

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="instruction.updated",
                user_id=str(current_user.id),
                resource_type="instruction",
                resource_id=str(instruction.id),
                details={"title": instruction.title, "category": instruction.category},
            )
        except Exception:
            pass

        return await self._instruction_to_schema_with_references(db, instruction)

    async def revert_instruction_to_version(
        self,
        db: AsyncSession,
        instruction_id: str,
        version_id: str,
        organization: Organization,
        current_user: User,
    ) -> Optional[InstructionSchema]:
        """Revert an instruction to a prior version by creating a NEW version
        that copies the target version's content and adding it to a draft build.

        Admin-only. The version's status field is copied as-is — what is
        actually live is gated by the build promotion (see BuildService.promote_build),
        not the version's status field.
        """
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        if not self._is_admin_permissions(user_permissions):
            raise HTTPException(status_code=403, detail="Permission denied: admin only")

        instruction = await self._get_instruction_by_id(db, instruction_id, organization)
        if not instruction:
            return None

        target_version = await self.version_service.get_version(db, version_id)
        if not target_version or target_version.instruction_id != instruction.id:
            raise HTTPException(status_code=404, detail="Version not found for this instruction")

        # Create a new version copying everything from the target version.
        # Audit trail is preserved: the new version has its own version_number,
        # created_by_user_id, and created_at — readers can see "v7 created by X
        # at time T with same content as v3".
        new_version = await self.version_service.create_version_from_data(
            db,
            instruction_id=instruction.id,
            text=target_version.text,
            title=target_version.title,
            structured_data=target_version.structured_data,
            formatted_content=target_version.formatted_content,
            status=target_version.status or 'published',
            load_mode=target_version.load_mode or 'always',
            references_json=target_version.references_json,
            data_source_ids=target_version.data_source_ids,
            label_ids=target_version.label_ids,
            category_ids=target_version.category_ids,
            user_id=current_user.id,
        )

        # Sync the live row so non-version-aware readers see the new content
        # immediately on promotion. (current_version_id is updated by the build
        # promote step; field sync also happens there. Setting here is safe
        # because the build will overwrite with the same values.)
        instruction.current_version_id = new_version.id
        await db.commit()

        # Stage in a draft build and auto-finalize. Admins auto-promote to main;
        # if anything is unusual the build sits in pending_approval and the
        # banner in ReportAgentPanel will surface it.
        build = await self.build_service.get_or_create_draft_build(
            db, organization.id, source='user', user_id=current_user.id
        )
        await self.build_service.add_to_build(
            db, build.id, instruction.id, new_version.id
        )
        await db.commit()
        await self._auto_finalize_build(db, build, current_user, user_permissions)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="instruction.reverted",
                user_id=str(current_user.id),
                resource_type="instruction",
                resource_id=str(instruction.id),
                details={
                    "from_version_id": str(target_version.id),
                    "from_version_number": target_version.version_number,
                    "new_version_id": str(new_version.id),
                    "new_version_number": new_version.version_number,
                },
            )
        except Exception:
            pass

        # Return the refreshed instruction
        return await self.get_instruction(db, instruction.id, organization, current_user)

    async def resolve_suggestion(
        self,
        db: AsyncSession,
        instruction_id: str,
        *,
        build_id: Optional[str],
        promote_text: str,
        remaining_text: str,
        title: Optional[str],
        organization: Organization,
        current_user: User,
    ) -> Optional[InstructionSchema]:
        """Apply a per-hunk resolution of a suggested change.

        ``promote_text`` (current + accepted hunks) is promoted as a fresh
        build-of-one when it differs from the live text. ``remaining_text``
        (current + still-pending hunks) is what stays proposed on the source
        suggestion build; when it equals the live text the instruction is
        dropped from that build (the suggestion is fully resolved).
        """
        user_permissions = await self._get_user_permissions(db, current_user, organization)

        instruction = await self._get_instruction_by_id(db, instruction_id, organization)
        if not instruction:
            return None

        # Resolve the live (current) text and the metadata to carry forward.
        meta_src = None
        if instruction.current_version_id:
            meta_src = await self.version_service.get_version(db, instruction.current_version_id)
        cur_text = (meta_src.text if meta_src else None)
        if cur_text is None:
            cur_text = getattr(instruction, 'text', '') or ''

        async def _mk_version(text: str):
            return await self.version_service.create_version_from_data(
                db,
                instruction_id=instruction.id,
                text=text,
                title=title if title is not None else (meta_src.title if meta_src else getattr(instruction, 'title', None)),
                structured_data=(meta_src.structured_data if meta_src else None),
                formatted_content=None,
                status=(meta_src.status if meta_src else 'published') or 'published',
                load_mode=(meta_src.load_mode if meta_src else getattr(instruction, 'load_mode', 'always')) or 'always',
                references_json=(meta_src.references_json if meta_src else None),
                data_source_ids=(meta_src.data_source_ids if meta_src else None),
                label_ids=(meta_src.label_ids if meta_src else None),
                category_ids=(meta_src.category_ids if meta_src else None),
                user_id=current_user.id,
            )

        # 1) Promote the accepted hunks as an isolated build-of-one. We use a
        #    fresh build (not the shared user draft) so only THIS instruction's
        #    change is promoted; everything else inherits main unchanged.
        if (promote_text or '').strip() != (cur_text or '').strip():
            new_version = await _mk_version(promote_text)
            build = await self.build_service.create_build(
                db, organization.id, source='user', user_id=current_user.id, copy_from_main=True
            )
            await self.build_service.add_to_build(db, build.id, instruction.id, new_version.id)
            await db.commit()
            await self._auto_finalize_build(db, build, current_user, user_permissions)
            cur_text = promote_text

        # 2) Reconcile the source suggestion build against what's left pending.
        if build_id:
            src_build = await self.build_service.get_build(db, build_id)
            if src_build and str(src_build.organization_id) == str(organization.id) and not src_build.is_main:
                if (remaining_text or '').strip() == (cur_text or '').strip():
                    # Nothing left to propose — drop this instruction from the
                    # suggestion. Other instructions in the build are untouched.
                    try:
                        await self.build_service.remove_from_build(db, build_id, instruction.id)
                    except Exception:
                        pass
                    # The suggestion is fully handled — clear it from the Review feed.
                    try:
                        from app.services.review_service import review_service
                        await review_service.resolve_for_instruction(
                            db, organization_id=str(organization.id), instruction_id=str(instruction.id),
                        )
                    except Exception:
                        pass
                else:
                    # Persist the shrunken proposal so the remaining hunks keep
                    # showing (diffed against the new current text).
                    rem_version = await _mk_version(remaining_text)
                    await self.build_service.add_to_build(db, build_id, instruction.id, rem_version.id)
                    await db.commit()

        return await self.get_instruction(db, instruction.id, organization, current_user)

    # ── Per-hunk tracked changes (immutable cherry-pick model) ────────────────
    async def _pending_suggestion_builds(self, db: AsyncSession, instruction_id: str, organization: Organization):
        """Non-main draft/pending builds (user|ai|git) that contain this
        instruction, newest first. Each is an immutable suggestion snapshot."""
        from app.models.instruction_build import InstructionBuild
        from app.models.build_content import BuildContent
        from app.models.instruction_version import InstructionVersion as _IV
        rows = (await db.execute(
            select(InstructionBuild, _IV.text, _IV.id)
            .join(BuildContent, BuildContent.build_id == InstructionBuild.id)
            .join(_IV, _IV.id == BuildContent.instruction_version_id)
            .options(selectinload(InstructionBuild.created_by_user))
            .where(
                BuildContent.instruction_id == instruction_id,
                InstructionBuild.organization_id == str(organization.id),
                InstructionBuild.is_main == False,  # noqa: E712
                InstructionBuild.deleted_at == None,  # noqa: E711
                InstructionBuild.status.in_(["draft", "pending_approval"]),
                InstructionBuild.source.in_(["user", "ai", "git"]),
            )
            .order_by(InstructionBuild.created_at.desc())
        )).all()
        return rows

    async def _main_text_of(self, db: AsyncSession, instruction):
        """Current live (main) text + version id of an instruction, read
        AUTHORITATIVELY from the is_main build's content. (Instruction.current_
        version_id is updated by promote via raw SQL and can be stale in a cached
        session; the is_main build query always reflects committed state.)"""
        from app.models.instruction_build import InstructionBuild
        from app.models.build_content import BuildContent
        from app.models.instruction_version import InstructionVersion as _IV
        row = (await db.execute(
            select(_IV.id, _IV.text)
            .join(BuildContent, BuildContent.instruction_version_id == _IV.id)
            .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
            .where(
                InstructionBuild.is_main == True,  # noqa: E712
                InstructionBuild.organization_id == str(instruction.organization_id),
                InstructionBuild.deleted_at == None,  # noqa: E711
                BuildContent.instruction_id == str(instruction.id),
            ).limit(1)
        )).first()
        if row:
            main_vid = str(row[0])
            meta_src = await self.version_service.get_version(db, main_vid)
            return (row[1] or ""), main_vid, meta_src
        # Fallback: no main build content (legacy) — use the cached row / version.
        meta_src = None
        if instruction.current_version_id:
            meta_src = await self.version_service.get_version(db, instruction.current_version_id)
        main_text = (meta_src.text if meta_src else getattr(instruction, "text", "")) or ""
        main_vid = str(instruction.current_version_id) if instruction.current_version_id else None
        return main_text, main_vid, meta_src

    @staticmethod
    def _rejected_keys(build, instruction_id: str) -> set:
        return {
            r.get("key")
            for r in (getattr(build, "rejected_hunks", None) or [])
            if r.get("instruction_id") == str(instruction_id)
        }

    async def _build_base_text(self, db: AsyncSession, build, instruction_id: str) -> str:
        """Text of `instruction_id` in this build's base (what the suggestion
        forked from). '' if no base / instruction absent from base (added new)."""
        base_id = getattr(build, "base_build_id", None)
        if not base_id:
            return ""
        txt = await self._build_instruction_text(db, str(base_id), instruction_id)
        return txt or ""

    async def review_hunks(self, db: AsyncSession, instruction_id: str, *, organization: Organization, current_user: User):
        """Per-instruction tracked changes. For each pending suggestion, compute
        its INTENT hunks (diff base->proposed), then keep only those that (a)
        aren't rejected, (b) apply cleanly onto current main (no conflict), and
        (c) actually change main (not already applied). Each surfaced hunk is
        rendered against current main so the UI overlays it on live text."""
        from app.services.text_hunks import rebased_hunks_against_main
        from app.models.agent_execution import AgentExecution
        instruction = await self._get_instruction_by_id(db, instruction_id, organization)
        if not instruction:
            return None
        main_text, main_vid, _meta = await self._main_text_of(db, instruction)
        rows = await self._pending_suggestion_builds(db, instruction_id, organization)
        suggestions = []
        for build, proposed_text, proposed_vid in rows:
            rejected = self._rejected_keys(build, instruction_id)
            base_text = await self._build_base_text(db, build, instruction_id)
            # Lenient: rebase the suggestion's intent onto current main so a STALE
            # suggestion (forked from an older main) still surfaces reviewable
            # hunks instead of collapsing to nothing. Identical to the strict
            # path when main hasn't drifted.
            shown = [h for h in rebased_hunks_against_main(base_text, proposed_text or "", main_text)
                     if h["key"] not in rejected]
            if not shown:
                continue
            trace = None
            if getattr(build, "agent_execution_id", None):
                ex = (await db.execute(
                    select(AgentExecution.report_id, AgentExecution.completion_id)
                    .where(AgentExecution.id == str(build.agent_execution_id))
                )).first()
                if ex:
                    trace = {"report_id": str(ex[0]) if ex[0] else None, "completion_id": str(ex[1]) if ex[1] else None}
            creator = getattr(build, "created_by_user", None)
            suggestions.append({
                "build_id": str(build.id),
                "build_number": build.build_number,
                "source": build.source,
                "created_at": build.created_at.isoformat() if build.created_at else None,
                "created_by": ({"id": str(creator.id), "name": getattr(creator, "name", None) or getattr(creator, "email", None)} if creator else None),
                "proposed_version_id": str(proposed_vid),
                "message": build.description,
                "report_id": (trace or {}).get("report_id"),
                "completion_id": (trace or {}).get("completion_id"),
                "hunks": shown,
            })
        return {"main_version_id": main_vid, "main_text": main_text, "suggestions": suggestions}

    async def accept_hunk(self, db: AsyncSession, instruction_id: str, *, build_id: str, hunk_key: str,
                          against_main_version_id: Optional[str], organization: Organization, current_user: User):
        """Cherry-pick one hunk of a suggestion onto main: create a NEW build =
        main + that hunk and promote it. The suggestion build is never mutated;
        because main then contains the change, the hunk drops out of its review."""
        from app.services.text_hunks import rebased_hunks_against_main
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        instruction = await self._get_instruction_by_id(db, instruction_id, organization)
        if not instruction:
            return None, "not_found"
        main_text, main_vid, meta_src = await self._main_text_of(db, instruction)
        # Optimistic concurrency: the client diffed against a specific main.
        if against_main_version_id and main_vid and str(against_main_version_id) != str(main_vid):
            return None, "conflict"
        build = await self.build_service.get_build(db, build_id)
        if not build or str(build.organization_id) != str(organization.id):
            return None, "not_found"
        proposed = await self._build_instruction_text(db, build_id, instruction_id)
        if proposed is None:
            return None, "not_found"
        base_text = await self._build_base_text(db, build, instruction_id)
        # Locate the hunk among the (rebased) hunks shown for review and splice it
        # in by char offset — works for both clean and stale (rebased) suggestions.
        rh = next((h for h in rebased_hunks_against_main(base_text, proposed or "", main_text)
                   if h["key"] == hunk_key), None)
        if rh is None:
            return None, "conflict"
        new_text = main_text[:rh["start"]] + rh["after"] + main_text[rh["end"]:]
        if new_text == main_text:
            return await self.get_instruction(db, instruction.id, organization, current_user), "noop"
        # Promote new build-of-one (only this instruction changes; rest inherit main).
        new_version = await self.version_service.create_version_from_data(
            db, instruction_id=instruction.id, text=new_text,
            title=(meta_src.title if meta_src else getattr(instruction, "title", None)),
            structured_data=(meta_src.structured_data if meta_src else None), formatted_content=None,
            status=(meta_src.status if meta_src else "published") or "published",
            load_mode=(meta_src.load_mode if meta_src else getattr(instruction, "load_mode", "always")) or "always",
            references_json=(meta_src.references_json if meta_src else None),
            data_source_ids=(meta_src.data_source_ids if meta_src else None),
            label_ids=(meta_src.label_ids if meta_src else None),
            category_ids=(meta_src.category_ids if meta_src else None),
            user_id=current_user.id,
        )
        new_build = await self.build_service.create_build(db, organization.id, source="user", user_id=current_user.id, copy_from_main=True)
        await self.build_service.add_to_build(db, new_build.id, instruction.id, new_version.id)
        await db.commit()
        await self._auto_finalize_build(db, new_build, current_user, user_permissions, trigger_reliability=False)
        # Record the accepted hunk on the SOURCE suggestion as resolved, so it
        # stays resolved even if main later drifts around its anchor (the build's
        # content snapshot is untouched — this is review metadata only).
        await self._record_resolved_hunk(db, build, instruction_id, hunk_key, "accept")
        return await self.get_instruction(db, instruction.id, organization, current_user), "ok"

    async def _record_resolved_hunk(self, db: AsyncSession, build, instruction_id: str, hunk_key: str, action: str, *, commit: bool = True):
        from sqlalchemy.orm.attributes import flag_modified
        rej = list(build.rejected_hunks or [])
        if not any(r.get("key") == hunk_key and r.get("instruction_id") == str(instruction_id) for r in rej):
            rej.append({"instruction_id": str(instruction_id), "key": hunk_key, "action": action})
            build.rejected_hunks = rej
            flag_modified(build, "rejected_hunks")
            if commit:
                await db.commit()

    async def accept_all_hunks(self, db: AsyncSession, instruction_id: str, *,
                               against_main_version_id: Optional[str], organization: Organization, current_user: User):
        """Accept EVERY live hunk in one pass: apply them all cumulatively onto
        main (newest suggestion wins on overlap), promote a SINGLE new build, and
        record every accepted hunk. One request, one build — no per-hunk churn."""
        from app.services.text_hunks import rebased_hunks_against_main
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        instruction = await self._get_instruction_by_id(db, instruction_id, organization)
        if not instruction:
            return None, "not_found"
        main_text, main_vid, meta_src = await self._main_text_of(db, instruction)
        if against_main_version_id and main_vid and str(against_main_version_id) != str(main_vid):
            return None, "conflict"
        rows = await self._pending_suggestion_builds(db, instruction_id, organization)  # newest first
        new_text = main_text
        accepted = []  # (build, key)
        for build, proposed_text, _vid in rows:
            rejected = self._rejected_keys(build, instruction_id)
            base_text = await self._build_base_text(db, build, instruction_id)
            rhs = [h for h in rebased_hunks_against_main(base_text, proposed_text or "", new_text)
                   if h["key"] not in rejected]
            # Apply this suggestion's non-rejected hunks right-to-left so earlier
            # char offsets stay valid; the next suggestion rebases onto the result.
            for h in sorted(rhs, key=lambda x: x["start"], reverse=True):
                new_text = new_text[:h["start"]] + h["after"] + new_text[h["end"]:]
                accepted.append((build, h["key"]))
        if not accepted or new_text == main_text:
            return await self.get_instruction(db, instruction.id, organization, current_user), "noop"
        new_version = await self.version_service.create_version_from_data(
            db, instruction_id=instruction.id, text=new_text,
            title=(meta_src.title if meta_src else getattr(instruction, "title", None)),
            structured_data=(meta_src.structured_data if meta_src else None), formatted_content=None,
            status=(meta_src.status if meta_src else "published") or "published",
            load_mode=(meta_src.load_mode if meta_src else getattr(instruction, "load_mode", "always")) or "always",
            references_json=(meta_src.references_json if meta_src else None),
            data_source_ids=(meta_src.data_source_ids if meta_src else None),
            label_ids=(meta_src.label_ids if meta_src else None),
            category_ids=(meta_src.category_ids if meta_src else None),
            user_id=current_user.id,
        )
        nb = await self.build_service.create_build(db, organization.id, source="user", user_id=current_user.id, copy_from_main=True)
        await self.build_service.add_to_build(db, nb.id, instruction.id, new_version.id)
        await db.commit()
        await self._auto_finalize_build(db, nb, current_user, user_permissions, trigger_reliability=False)
        for build, key in accepted:
            await self._record_resolved_hunk(db, build, instruction_id, key, "accept", commit=False)
        await db.commit()
        return await self.get_instruction(db, instruction.id, organization, current_user), "ok"

    async def reject_all_hunks(self, db: AsyncSession, instruction_id: str, *,
                               organization: Organization, current_user: User):
        """Reject every live hunk in one pass (records them; main unchanged)."""
        from app.services.text_hunks import rebased_hunks_against_main
        instruction = await self._get_instruction_by_id(db, instruction_id, organization)
        if not instruction:
            return None, "not_found"
        main_text, _vid, _meta = await self._main_text_of(db, instruction)
        rows = await self._pending_suggestion_builds(db, instruction_id, organization)
        for build, proposed_text, _v in rows:
            rejected = self._rejected_keys(build, instruction_id)
            base_text = await self._build_base_text(db, build, instruction_id)
            for h in rebased_hunks_against_main(base_text, proposed_text or "", main_text):
                if h["key"] in rejected:
                    continue
                await self._record_resolved_hunk(db, build, instruction_id, h["key"], "reject", commit=False)
        await db.commit()
        try:
            from app.services.review_service import review_service
            await review_service.resolve_for_instruction(db, organization_id=str(organization.id), instruction_id=str(instruction_id))
        except Exception:
            pass
        return await self.get_instruction(db, instruction.id, organization, current_user), "ok"

    async def reject_hunk(self, db: AsyncSession, instruction_id: str, *, build_id: str, hunk_key: str,
                          organization: Organization, current_user: User):
        """Record a hunk as rejected on the suggestion build (the only persisted
        per-hunk state). The build's content snapshot is left immutable."""
        build = await self.build_service.get_build(db, build_id)
        if not build or str(build.organization_id) != str(organization.id) or build.is_main:
            return None, "not_found"
        await self._record_resolved_hunk(db, build, instruction_id, hunk_key, "reject")
        # If nothing remains pending for this instruction across all builds, clear
        # it from the Review feed.
        try:
            remaining = await self.review_hunks(db, instruction_id, organization=organization, current_user=current_user)
            if remaining and not remaining.get("suggestions"):
                from app.services.review_service import review_service
                await review_service.resolve_for_instruction(db, organization_id=str(organization.id), instruction_id=str(instruction_id))
        except Exception:
            pass
        return await self.get_instruction(db, instruction_id, organization, current_user), "ok"

    async def _build_instruction_text(self, db: AsyncSession, build_id: str, instruction_id: str) -> Optional[str]:
        """Text of `instruction_id`'s version inside `build_id` (the proposed snapshot)."""
        from app.models.build_content import BuildContent
        from app.models.instruction_version import InstructionVersion as _IV
        row = (await db.execute(
            select(_IV.text).join(BuildContent, BuildContent.instruction_version_id == _IV.id)
            .where(BuildContent.build_id == build_id, BuildContent.instruction_id == instruction_id)
            .limit(1)
        )).first()
        return (row[0] or "") if row else None

    async def enhance_instruction(
        self,
        db: AsyncSession, 
        instruction_data: InstructionCreate, 
        organization: Organization,
        current_user: User
    ) -> Optional[str]:
        """Enhance an instruction with AI"""
        instruction_text = instruction_data.text
        
        data_source_context = await self._build_data_source_context(
            db,
            organization,
            instruction_data.data_source_ids or []
        )

        instructions_builder = InstructionContextBuilder(db, organization)
        instructions_context = await instructions_builder.build()
        instructions_context = instructions_context.render()

        small_model = await self.llm_service.get_default_model(db, organization, current_user, is_small=True)
        org_settings = await OrganizationSettingsService().get_settings(db, organization, current_user)
        suggest_instructions = SuggestInstructions(
            model=small_model,
            organization_settings=org_settings,
            usage_session_maker=async_session_maker,
        )
        enhanced_instruction_text = await suggest_instructions.enhance_instruction(
            instruction_text,
            instructions_context,
            data_source_context
        )

        
        if not enhanced_instruction_text:
            raise HTTPException(status_code=400, detail="Failed to enhance instruction")
        
        return enhanced_instruction_text.get("enhanced_instruction", None)




    async def delete_instruction(
        self,
        db: AsyncSession,
        instruction_id: str,
        organization: Organization,
        current_user: User,
    ) -> bool:
        """Delete an instruction (soft delete)"""
        
        # Get user permissions for auto-publish check
        user_permissions = await self._get_user_permissions(db, current_user, organization)

        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id
                )
            )
        )
        instruction = result.scalar_one_or_none()

        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")

        # Permission check is handled by the decorator, so we can proceed with deletion
        # Soft delete (using BaseSchema's soft delete functionality)
        from datetime import datetime
        instruction.deleted_at = datetime.utcnow()
        await db.commit()
        
        # === Build System Integration ===
        # Create a new build that removes this instruction
        try:
            build = await self.build_service.get_or_create_draft_build(
                db, organization.id, source='user', user_id=current_user.id
            )
            
            # Remove the instruction from the build
            await self.build_service.remove_from_build(db, build.id, instruction_id)
            await db.commit()
            
            # Auto-finalize to reflect deletion in main build
            await self._auto_finalize_build(db, build, current_user, user_permissions)
            
            # Auto-promote delete builds since the delete already took effect via deleted_at
            await db.refresh(build)
            if build.status == 'approved' and not build.is_main:
                await self.build_service.promote_build(db, build.id)
                logger.debug(f"Auto-promoted delete build {build.id} to main")
            
            logger.debug(f"Removed instruction {instruction_id} from build {build.id}")
        except Exception as e:
            logger.warning(f"Failed to update build for deleted instruction {instruction_id}: {e}")
            # Don't fail the deletion if build update fails

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="instruction.deleted",
                user_id=str(current_user.id),
                resource_type="instruction",
                resource_id=str(instruction.id),
                details={"title": instruction.title},
            )
        except Exception:
            pass

        return True
    
    async def increment_thumbs_up(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        organization: Organization,
        current_user: User
    ) -> InstructionSchema:
        """Increment thumbs up count for an instruction"""
        
        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id
                )
            )
        )
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")
        
        instruction.thumbs_up += 1
        await db.commit()
        await db.refresh(instruction, ["user", "data_sources", "reviewed_by"])
        return InstructionSchema.from_orm(instruction)

    async def bulk_update_instructions(
        self,
        db: AsyncSession,
        bulk_update,  # InstructionBulkUpdate
        current_user: User,
        organization: Organization,
    ) -> dict:
        """Bulk update multiple instructions (admin only)"""
        from app.schemas.instruction_schema import InstructionBulkResponse
        from app.models.instruction_label import InstructionLabel
        
        # Get user permissions for auto-publish check
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        
        updated_count = 0
        failed_ids = []
        
        # Fetch all instructions by IDs with relationships needed for versioning
        result = await db.execute(
            select(Instruction)
            .options(
                selectinload(Instruction.labels),
                selectinload(Instruction.data_sources),
                selectinload(Instruction.references),
            )
            .where(
                and_(
                    Instruction.id.in_(bulk_update.ids),
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        instructions = result.scalars().all()
        
        # Build a set of found IDs
        found_ids = {str(inst.id) for inst in instructions}
        
        # Track which IDs were not found
        for req_id in bulk_update.ids:
            if req_id not in found_ids:
                failed_ids.append(req_id)
        
        # Fetch labels if needed
        labels_to_set = None  # None means no change, empty list means clear labels
        labels_to_add = []
        labels_to_remove_ids = set()
        
        if bulk_update.set_label_ids is not None:  # Empty list is valid (= clear labels)
            if bulk_update.set_label_ids:
                label_result = await db.execute(
                    select(InstructionLabel).where(
                        and_(
                            InstructionLabel.id.in_(bulk_update.set_label_ids),
                            InstructionLabel.organization_id == organization.id
                        )
                    )
                )
                labels_to_set = label_result.scalars().all()
            else:
                labels_to_set = []  # Clear all labels
        
        if bulk_update.add_label_ids:
            label_result = await db.execute(
                select(InstructionLabel).where(
                    and_(
                        InstructionLabel.id.in_(bulk_update.add_label_ids),
                        InstructionLabel.organization_id == organization.id
                    )
                )
            )
            labels_to_add = label_result.scalars().all()
        
        if bulk_update.remove_label_ids:
            labels_to_remove_ids = set(bulk_update.remove_label_ids)
        
        # Fetch data sources if needed for scope updates
        data_sources_to_set = None  # None means no change, empty list means make global
        data_sources_to_add = []
        data_sources_to_remove_ids = set()
        
        if bulk_update.set_data_source_ids is not None:  # Empty list is valid (= make global)
            if bulk_update.set_data_source_ids:
                ds_result = await db.execute(
                    select(DataSource).where(
                        and_(
                            DataSource.id.in_(bulk_update.set_data_source_ids),
                            DataSource.organization_id == organization.id
                        )
                    )
                )
                data_sources_to_set = ds_result.scalars().all()
            else:
                data_sources_to_set = []  # Clear all = make global
        
        if bulk_update.add_data_source_ids:
            ds_result = await db.execute(
                select(DataSource).where(
                    and_(
                        DataSource.id.in_(bulk_update.add_data_source_ids),
                        DataSource.organization_id == organization.id
                    )
                )
            )
            data_sources_to_add = ds_result.scalars().all()
        
        if bulk_update.remove_data_source_ids:
            data_sources_to_remove_ids = set(bulk_update.remove_data_source_ids)
        
        # === Build System Integration ===
        # Create a single build for all bulk updates
        bulk_build = None
        try:
            bulk_build = await self.build_service.get_or_create_draft_build(
                db, organization.id, source='user', user_id=current_user.id
            )
            logger.debug(f"Created bulk update build {bulk_build.id}")
        except Exception as build_error:
            logger.warning(f"Failed to create bulk update build: {build_error}")
        
        # Track instructions that were actually modified for versioning
        # Only content changes (status, load_mode, data_sources) trigger builds
        # Label changes are metadata only - no build needed
        modified_instructions = []
        
        # Apply updates
        for instruction in instructions:
            try:
                content_modified = False  # Changes that need builds (status, load_mode, data_sources)
                metadata_modified = False  # Changes that don't need builds (labels)
                
                # Update status (simplified - no dual-status handling) - CONTENT CHANGE
                if bulk_update.status:
                    instruction.status = bulk_update.status
                    content_modified = True
                
                # Update load mode - CONTENT CHANGE
                if bulk_update.load_mode:
                    instruction.load_mode = bulk_update.load_mode
                    content_modified = True

                # Skills always use 'intelligent' (smart) retrieval. Override any
                # bulk-applied load_mode for skills so they can't be forced to
                # 'always'/'disabled'.
                if getattr(instruction, "kind", "instruction") == "skill" \
                        and instruction.load_mode != "intelligent":
                    instruction.load_mode = "intelligent"
                    content_modified = True

                # Set labels (replace all) - METADATA ONLY, no build
                if labels_to_set is not None:
                    instruction.labels = list(labels_to_set)
                    metadata_modified = True
                
                # Add labels - METADATA ONLY, no build
                for label in labels_to_add:
                    if label not in instruction.labels:
                        instruction.labels.append(label)
                        metadata_modified = True
                
                # Remove labels - METADATA ONLY, no build
                if labels_to_remove_ids:
                    original_count = len(instruction.labels)
                    instruction.labels = [
                        lbl for lbl in instruction.labels 
                        if str(lbl.id) not in labels_to_remove_ids
                    ]
                    if len(instruction.labels) != original_count:
                        metadata_modified = True
                
                # Set data sources (replace all) - CONTENT CHANGE
                if data_sources_to_set is not None:
                    instruction.data_sources = list(data_sources_to_set)
                    content_modified = True
                
                # Add data sources - CONTENT CHANGE
                for ds in data_sources_to_add:
                    if ds not in instruction.data_sources:
                        instruction.data_sources.append(ds)
                        content_modified = True
                
                # Remove data sources - CONTENT CHANGE
                if data_sources_to_remove_ids:
                    original_count = len(instruction.data_sources)
                    instruction.data_sources = [
                        ds for ds in instruction.data_sources 
                        if str(ds.id) not in data_sources_to_remove_ids
                    ]
                    if len(instruction.data_sources) != original_count:
                        content_modified = True
                
                # Only track for build if content was modified
                if content_modified:
                    modified_instructions.append(instruction)
                
                # Count as updated if anything changed
                if content_modified or metadata_modified:
                    updated_count += 1
            except Exception as e:
                failed_ids.append(str(instruction.id))
        
        await db.commit()
        
        # === Create versions and add to build ===
        if bulk_build and modified_instructions:
            try:
                for instruction in modified_instructions:
                    # Re-fetch with fresh relationships
                    fresh_result = await db.execute(
                        select(Instruction)
                        .options(
                            selectinload(Instruction.labels),
                            selectinload(Instruction.data_sources),
                            selectinload(Instruction.references),
                        )
                        .where(Instruction.id == instruction.id)
                    )
                    fresh_instruction = fresh_result.scalar_one()
                    
                    # Create version
                    version = await self.version_service.create_version(
                        db, fresh_instruction, user_id=current_user.id
                    )
                    fresh_instruction.current_version_id = version.id
                    
                    # Add to build
                    await self.build_service.add_to_build(
                        db, bulk_build.id, fresh_instruction.id, version.id
                    )
                
                await db.commit()
                
                # Finalize the build
                await self._auto_finalize_build(db, bulk_build, current_user, user_permissions)
                logger.debug(f"Finalized bulk update build {bulk_build.id} with {len(modified_instructions)} instructions")
            except Exception as version_error:
                logger.warning(f"Failed to create versions for bulk update: {version_error}")
        
        return InstructionBulkResponse(
            updated_count=updated_count,
            failed_ids=failed_ids,
            message=f"Successfully updated {updated_count} instructions"
        )

    async def bulk_delete_instructions(
        self,
        db: AsyncSession,
        instruction_ids: List[str],
        current_user: User,
        organization: Organization,
    ) -> dict:
        """Bulk delete multiple instructions (soft delete) with a single build"""
        from app.schemas.instruction_schema import InstructionBulkResponse
        
        # Get user permissions for auto-publish check
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        
        deleted_count = 0
        failed_ids = []
        
        # Fetch all instructions by IDs
        result = await db.execute(
            select(Instruction)
            .where(
                and_(
                    Instruction.id.in_(instruction_ids),
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        instructions = result.scalars().all()
        
        # Track which IDs were not found
        found_ids = {str(inst.id) for inst in instructions}
        for req_id in instruction_ids:
            if req_id not in found_ids:
                failed_ids.append(req_id)
        
        # === Build System Integration ===
        # Create a single build for all bulk deletions
        bulk_build = None
        try:
            bulk_build = await self.build_service.get_or_create_draft_build(
                db, organization.id, source='user', user_id=current_user.id
            )
            logger.debug(f"Created bulk delete build {bulk_build.id}")
        except Exception as build_error:
            logger.warning(f"Failed to create bulk delete build: {build_error}")
        
        # Apply soft deletes
        for instruction in instructions:
            try:
                instruction.deleted_at = datetime.utcnow()
                
                # Remove from build if we have one
                if bulk_build:
                    try:
                        await self.build_service.remove_from_build(db, bulk_build.id, str(instruction.id))
                    except Exception as e:
                        logger.warning(f"Failed to remove instruction {instruction.id} from build: {e}")
                
                deleted_count += 1
            except Exception as e:
                failed_ids.append(str(instruction.id))
                logger.warning(f"Failed to delete instruction {instruction.id}: {e}")
        
        await db.commit()
        
        # Finalize and promote the build (delete is immediate, so build should reflect that)
        if bulk_build:
            try:
                await self._auto_finalize_build(db, bulk_build, current_user, user_permissions)
                
                # Auto-promote delete builds since the delete already took effect via deleted_at
                await db.refresh(bulk_build)
                if bulk_build.status == 'approved' and not bulk_build.is_main:
                    await self.build_service.promote_build(db, bulk_build.id)
                    logger.debug(f"Auto-promoted bulk delete build {bulk_build.id} to main")
            except Exception as finalize_error:
                logger.warning(f"Failed to finalize/promote bulk delete build: {finalize_error}")
        
        return InstructionBulkResponse(
            updated_count=deleted_count,
            failed_ids=failed_ids,
            message=f"Successfully deleted {deleted_count} instructions"
        )
    
    async def get_instructions_for_data_source(
        self, 
        db: AsyncSession, 
        data_source_id: str, 
        organization: Organization,
        current_user: User,
        status: str = "published"
    ) -> List[InstructionListSchema]:
        """Get all instructions that apply to a specific data source (including global ones)"""
        
        # Validate data source exists
        await self._validate_data_sources(db, [data_source_id], organization)
        
        query = (
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).options(
                    selectinload(DataSource.data_source_memberships),
                    selectinload(DataSource.primary_instruction),
                ),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
                selectinload(Instruction.labels),
            )
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.status == status,
                    Instruction.deleted_at == None,
                    # Either applies to this data source or is global (no data sources)
                    (Instruction.data_sources.any(DataSource.id == data_source_id)) |
                    (~Instruction.data_sources.any())
                )
            )
        ).order_by(Instruction.created_at.desc())
        
        result = await db.execute(query)
        instructions = result.scalars().all()
        schemas = [InstructionSchema.from_orm(instruction) for instruction in instructions]

        # Post-filter by per-user table accessibility
        if current_user:
            schemas = await self._filter_list_items_by_table_accessibility(
                db, schemas, str(current_user.id)
            )

        return schemas

    async def get_instructions_by_report(
        self,
        db: AsyncSession,
        report_id: str,
        organization: Organization,
    ) -> List[InstructionSchema]:
        """Get all instructions created OR edited during this report's agent sessions.

        Union of two sources:
        - Instructions whose `agent_execution_id` matches one of this report's
          agent executions (covers creates).
        - Instructions touched by any InstructionBuild whose `agent_execution_id`
          matches one of this report's agent executions (covers edits — an edit
          adds a new version to the build without changing the instruction row's
          original `agent_execution_id`).
        """
        from app.models.agent_execution import AgentExecution
        from app.models.instruction_build import InstructionBuild
        from app.models.build_content import BuildContent

        # 1. Instructions created in this report's sessions.
        created_subq = (
            select(Instruction.id)
            .join(AgentExecution, Instruction.agent_execution_id == AgentExecution.id)
            .where(AgentExecution.report_id == report_id)
        )

        # 2. Instructions edited in this report's sessions — i.e. BuildContent
        #    rows in a report-tied build whose (instruction_id, version_id)
        #    pair is NOT in the main build. Builds created by sessions inherit
        #    every main instruction via copy_from_main=True; without filtering
        #    that out we'd return every instruction in the org.
        from sqlalchemy.orm import aliased
        BC = aliased(BuildContent)
        MAIN_BC = aliased(BuildContent)
        main_build_ids_subq = (
            select(InstructionBuild.id).where(
                and_(
                    InstructionBuild.organization_id == organization.id,
                    InstructionBuild.is_main.is_(True),
                    InstructionBuild.deleted_at.is_(None),
                )
            )
        )
        edited_subq = (
            select(BC.instruction_id)
            .join(InstructionBuild, InstructionBuild.id == BC.build_id)
            .join(AgentExecution, AgentExecution.id == InstructionBuild.agent_execution_id)
            .outerjoin(
                MAIN_BC,
                and_(
                    MAIN_BC.instruction_id == BC.instruction_id,
                    MAIN_BC.instruction_version_id == BC.instruction_version_id,
                    MAIN_BC.build_id.in_(main_build_ids_subq),
                ),
            )
            .where(
                and_(
                    AgentExecution.report_id == report_id,
                    MAIN_BC.id.is_(None),  # pair not in main = a truly new version
                )
            )
        )

        # Eager-load every relationship InstructionSchema (and its nested
        # DataSourceSchema) touches; otherwise pydantic from_orm trips on
        # lazy='raise' relationships in the async session. Mirrors the option
        # set used by the singular get_instruction path.
        query = (
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).options(
                    lazyload("*"),
                    selectinload(DataSource.data_source_memberships),
                    selectinload(DataSource.git_repository),
                    selectinload(DataSource.connections).options(lazyload("*")),
                    selectinload(DataSource.primary_instruction),
                ),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
                selectinload(Instruction.labels),
            )
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None,
                    or_(
                        Instruction.id.in_(created_subq),
                        Instruction.id.in_(edited_subq),
                    ),
                )
            )
            .order_by(Instruction.created_at.asc())
        )

        result = await db.execute(query)
        instructions = result.scalars().all()
        return [
            await self._instruction_to_schema_with_references(db, instruction)
            for instruction in instructions
        ]

    async def _validate_data_sources(
        self,
        db: AsyncSession,
        data_source_ids: List[str],
        organization: Organization
    ):
        """Validate that all data source IDs exist and belong to the organization"""
        
        if not data_source_ids:
            return
        
        result = await db.execute(
            select(DataSource).where(
                and_(
                    DataSource.id.in_(data_source_ids),
                    DataSource.organization_id == organization.id
                )
            )
        )
        found_data_sources = result.scalars().all()
        
        if len(found_data_sources) != len(data_source_ids):
            found_ids = {ds.id for ds in found_data_sources}
            missing_ids = set(data_source_ids) - found_ids
            raise HTTPException(
                status_code=400, 
                detail=f"Data sources not found: {list(missing_ids)}"
            )
    
    async def _associate_data_sources(
        self, 
        db: AsyncSession, 
        instruction: Instruction, 
        data_source_ids: List[str]
    ):
        """Associate instruction with data sources"""
        
        if not data_source_ids:
            return
        
        # Get data source objects
        result = await db.execute(
            select(DataSource).where(DataSource.id.in_(data_source_ids))
        )
        data_sources = result.scalars().all()
        
        # Associate with instruction
        instruction.data_sources = data_sources
        await db.commit()
    
    async def _update_data_source_associations(
        self, 
        db: AsyncSession, 
        instruction: Instruction, 
        data_source_ids: List[str]
    ):
        """Update data source associations for an instruction"""
        
        # Clear existing associations
        instruction.data_sources.clear()
        
        # Add new associations if provided
        if data_source_ids:
            result = await db.execute(
                select(DataSource).where(DataSource.id.in_(data_source_ids))
            )
            data_sources = result.scalars().all()
            instruction.data_sources = data_sources
        
        await db.commit()

    async def _validate_labels(
        self,
        db: AsyncSession,
        label_ids: List[str],
        organization: Organization,
    ):
        """Validate that labels exist and belong to the organization."""
        if not label_ids:
            return

        requested_ids = {label_id for label_id in label_ids if label_id}
        if not requested_ids:
            return

        result = await db.execute(
            select(InstructionLabel).where(
                and_(
                    InstructionLabel.id.in_(requested_ids),
                    InstructionLabel.organization_id == organization.id,
                    InstructionLabel.deleted_at == None,
                )
            )
        )
        found_labels = result.scalars().all()
        found_ids = {label.id for label in found_labels}
        missing_ids = requested_ids - found_ids
        if missing_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Labels not found: {list(missing_ids)}",
            )

    async def _associate_labels(
        self,
        db: AsyncSession,
        instruction: Instruction,
        label_ids: List[str],
    ):
        """Associate instruction with labels during creation."""
        if not label_ids:
            return

        result = await db.execute(
            select(InstructionLabel).where(
                and_(
                    InstructionLabel.id.in_(label_ids),
                    InstructionLabel.organization_id == instruction.organization_id,
                    InstructionLabel.deleted_at == None,
                )
            )
        )
        labels = result.scalars().all()
        instruction.labels = labels
        await db.commit()

    async def _update_label_associations(
        self,
        db: AsyncSession,
        instruction: Instruction,
        label_ids: Optional[List[str]],
    ):
        """Replace label associations for an instruction."""
        instruction.labels.clear()

        if label_ids:
            result = await db.execute(
                select(InstructionLabel).where(
                    and_(
                        InstructionLabel.id.in_(label_ids),
                        InstructionLabel.organization_id == instruction.organization_id,
                        InstructionLabel.deleted_at == None,
                    )
                )
            )
            instruction.labels = result.scalars().all()

        await db.commit()

    async def _get_instruction_for_user(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        user: User, 
        organization: Organization
    ) -> Instruction:
        """Get instruction that belongs to the user"""
        
        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.user_id == user.id,
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")
        
        return instruction

    def _determine_update_type(self, instruction: Instruction, instruction_data: InstructionUpdate, current_user: User, user_permissions: set) -> str:
        """Determine what type of update this is based on permissions.

        MVP: no suggestion workflow. Admin (manage_instructions) edits anything;
        owner can edit their own; everyone else is denied.
        """
        is_admin = self._is_admin_permissions(user_permissions)
        is_owner = instruction.user_id == current_user.id

        if is_admin:
            return "admin_edit"
        if is_owner:
            return "owner_edit"
        return "no_permission"

    async def _handle_admin_edit(self, instruction: Instruction, instruction_data: InstructionUpdate, admin_user: User):
        """Handle admin editing any instruction (not review)"""
        
        # Admin can change status and gets credited as reviewer for status changes  
        if instruction_data.status and instruction_data.status != instruction.status:
            if instruction_data.status in ["published", "archived"]:
                instruction.reviewed_by_user_id = admin_user.id
        
        # Apply all changes (admin has full control)
        update_data = instruction_data.model_dump(exclude_unset=True, exclude={'data_source_ids', 'references'})
        for field, value in update_data.items():
            setattr(instruction, field, value)

    async def _handle_owner_edit(self, instruction: Instruction, instruction_data: InstructionUpdate):
        """Handle owner editing their own private instruction"""

        # Owner can only edit text/title/category/toggles. Ignore any status changes silently.
        allowed_fields = ['text', 'title', 'category', 'kind', 'is_seen', 'can_user_toggle']
        
        # Apply allowed changes only (ignore status/private/global fields if present)
        for field in allowed_fields:
            if hasattr(instruction_data, field) and getattr(instruction_data, field) is not None:
                setattr(instruction, field, getattr(instruction_data, field))

    @staticmethod
    def _enforce_skill_load_mode(instruction: Instruction) -> None:
        """Skills always use 'intelligent' (smart) retrieval.

        A skill is meant to be surfaced contextually (advertised in the prompt and
        pulled on demand via read_instruction), never force-loaded. Enforce this
        server-side so the load_mode can never drift to 'always'/'disabled' for a
        skill regardless of what the caller passed.
        """
        if getattr(instruction, "kind", "instruction") == "skill":
            if instruction.load_mode != "intelligent":
                instruction.load_mode = "intelligent"

    def _is_admin_permissions(self, user_permissions: set) -> bool:
        """MVP: org-level manage_instructions is the admin gate."""
        return 'manage_instructions' in user_permissions or 'full_admin_access' in user_permissions

    async def _get_instruction_by_id(self, db: AsyncSession, instruction_id: str, organization: Organization) -> Instruction:
        """Get instruction by ID with proper error handling"""
        
        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")
        
        return instruction

    def _get_own_instructions_condition(self, user_id: str):
        """Simple condition for user's own instructions"""
        return Instruction.user_id == user_id

    def _get_others_instructions_condition(
        self, 
        user_id: str, 
        permissions: set, 
        include_drafts: bool, 
        include_archived: bool, 
        include_hidden: bool
    ):
        """Get condition for viewing others' instructions based on permissions.
        
        SIMPLIFIED: Visibility is controlled by the build system (whether instruction is in main build).
        We only filter by status here, not by deprecated private_status/global_status.
        """
        # Treat instructions with NULL user_id as "others" so system/AI-created drafts are visible
        base = [or_(Instruction.user_id != user_id, Instruction.user_id == None)]
        
        if 'manage_instructions' in permissions or 'full_admin_access' in permissions:
            # Admin: see everything with optional filters
            if not include_drafts:
                base.append(Instruction.status != "draft")
            if not include_archived:
                base.append(Instruction.status != "archived")
            if not include_hidden:
                base.append(Instruction.is_seen == True)
            return and_(*base)

        # Non-admin org members: only published, visible instructions.
        # Per-DS visibility is enforced at the route layer via DS resource grants.
        base.extend([
            Instruction.status == "published",
            Instruction.is_seen == True,
        ])
        return and_(*base)

    def _can_filter_by_user(self, permissions: set) -> bool:
        """Only org instruction admins may filter the list by arbitrary user id."""
        return 'manage_instructions' in permissions or 'full_admin_access' in permissions

    async def _execute_instructions_query(
        self,
        db: AsyncSession,
        organization: Organization,
        conditions: list,
        status: Optional[str],
        categories: Optional[List[str]],
        skip: int,
        limit: int,
        data_source_ids: Optional[List[str]] = None,
        source_types: Optional[List[str]] = None,
        load_modes: Optional[List[str]] = None,
        label_ids: Optional[List[str]] = None,
        search: Optional[str] = None,
        build_id: Optional[str] = None,
        include_global: bool = True,
        current_user: Optional[User] = None,
    ) -> dict:
        """Execute the instructions query with given conditions. Returns paginated response.

        By default, loads instructions from the main build (is_main=True).
        Pass build_id to load from a specific build instead.

        Every caller (admins included) only sees instructions that are global
        (attached to no data source) or attached to a data source they are an
        explicit member of (or that is public). Org admins do not get a blanket
        bypass here — the list mirrors the default data-sources view.
        """
        from sqlalchemy import func
        from app.models.instruction_label import InstructionLabel
        from app.models.metadata_resource import MetadataResource
        from app.models.instruction_build import InstructionBuild
        from app.models.build_content import BuildContent
        
        # Base query conditions
        base_conditions = [
            Instruction.organization_id == organization.id,
            Instruction.deleted_at == None
        ]
        
        # Get the target build (specific or main)
        target_build_id = build_id
        if not target_build_id:
            # Get the main build for this organization
            main_build_result = await db.execute(
                select(InstructionBuild.id).where(
                    and_(
                        InstructionBuild.organization_id == organization.id,
                        InstructionBuild.is_main == True,
                        InstructionBuild.deleted_at == None
                    )
                )
            )
            main_build = main_build_result.scalar_one_or_none()
            if main_build:
                target_build_id = main_build
        
        # If we have a target build, filter instructions to only those in the build
        if target_build_id:
            # Get instruction IDs that are in the target build
            build_instruction_ids_subquery = (
                select(BuildContent.instruction_id)
                .where(BuildContent.build_id == target_build_id)
            )
            base_conditions.append(Instruction.id.in_(build_instruction_ids_subquery))

        # Per-data-source visibility — applied to EVERYONE, admins included.
        # An instruction tied to a data source (agent) is only visible to users
        # who can actually access that data source; global instructions (no data
        # source) stay visible to all. This mirrors the default data-sources
        # list (see data_source_service.get_data_sources): the view is scoped to
        # *explicit* membership/grants even for admins — being an org admin does
        # not flood the table with instructions for agents you never joined. (An
        # admin retains capability bypass elsewhere and can still open any
        # instruction directly; this only governs the list.) Public data sources
        # are visible to every member.
        if current_user is not None:
            from app.core.permission_resolver import get_member_data_source_ids
            member_ds_ids = await get_member_data_source_ids(
                db, str(current_user.id), str(organization.id)
            )
            public_ds_subquery = (
                select(DataSource.id).where(
                    and_(
                        DataSource.organization_id == organization.id,
                        DataSource.is_public == True,
                    )
                )
            )
            visible_ds_clauses = [
                Instruction.data_sources.any(DataSource.id.in_(public_ds_subquery))
            ]
            if member_ds_ids:
                visible_ds_clauses.append(
                    Instruction.data_sources.any(DataSource.id.in_(member_ds_ids))
                )
            base_conditions.append(
                or_(
                    ~Instruction.data_sources.any(),  # global instruction
                    *visible_ds_clauses,
                )
            )

        # Build filter conditions list
        filter_conditions = []
        
        if status:
            filter_conditions.append(Instruction.status == status)
        if categories:
            filter_conditions.append(Instruction.category.in_(categories))
        if data_source_ids:
            # Filter by any of the specified domain IDs (OR logic)
            if include_global:
                # Include instructions that match the data sources OR have no data sources (global)
                filter_conditions.append(
                    or_(
                        Instruction.data_sources.any(DataSource.id.in_(data_source_ids)),
                        ~Instruction.data_sources.any()  # No data sources = global
                    )
                )
            else:
                filter_conditions.append(Instruction.data_sources.any(DataSource.id.in_(data_source_ids)))
        if source_types:
            # Build source type filter conditions
            # source_types can contain: 'user', 'ai', 'git', 'dbt', 'markdown', etc.
            # Supports both new flow (structured_data->>'resource_type') and legacy (MetadataResource)
            source_type_conditions = []
            for st in source_types:
                if st == 'user':
                    source_type_conditions.append(Instruction.source_type == 'user')
                elif st == 'ai':
                    source_type_conditions.append(Instruction.source_type == 'ai')
                elif st == 'git':
                    # All git-sourced instructions (dbt, markdown, etc.)
                    source_type_conditions.append(Instruction.source_type == 'git')
                elif st == 'dbt':
                    # Git instructions with dbt resource types (dbt_model, dbt_source, etc.)
                    source_type_conditions.append(
                        and_(
                            Instruction.source_type == 'git',
                            or_(
                                Instruction.structured_data['resource_type'].as_string().like('dbt_%'),
                                Instruction.source_metadata_resource.has(
                                    MetadataResource.resource_type.like('dbt_%')
                                ),
                            )
                        )
                    )
                elif st == 'markdown':
                    # Git instructions with markdown resource type
                    source_type_conditions.append(
                        and_(
                            Instruction.source_type == 'git',
                            or_(
                                Instruction.structured_data['resource_type'].as_string() == 'markdown_document',
                                Instruction.source_metadata_resource.has(
                                    MetadataResource.resource_type == 'markdown_document'
                                ),
                            )
                        )
                    )
            if source_type_conditions:
                filter_conditions.append(or_(*source_type_conditions))
        if load_modes:
            filter_conditions.append(Instruction.load_mode.in_(load_modes))
        if label_ids:
            filter_conditions.append(Instruction.labels.any(InstructionLabel.id.in_(label_ids)))
        if search:
            search_term = f"%{search.lower()}%"
            filter_conditions.append(
                or_(
                    func.lower(Instruction.text).like(search_term),
                    func.lower(Instruction.title).like(search_term)
                )
            )
        
        # Build the main query. lazyload("*") suppresses DataSource's
        # lazy="selectin" cascade (reports → widgets/queries/completions/…)
        # that would otherwise fire per loaded Instruction.data_sources.
        # The list response uses DataSourceMinimalSchema (id/name/description
        # only), so DS sub-relationships are pure waste.
        query = (
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).options(lazyload("*")),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.labels),
            )
            .where(and_(*base_conditions))
        )
        
        # Apply permission-based conditions
        if conditions:
            query = query.where(or_(*conditions))
        else:
            query = query.where(False)  # No access
        
        # Apply filter conditions
        for fc in filter_conditions:
            query = query.where(fc)
        
        # Count total before pagination
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        # Apply pagination and ordering
        query = query.offset(skip).limit(limit).order_by(Instruction.created_at.desc())
        
        result = await db.execute(query)
        instructions = result.scalars().all()
        
        # Map to list schema
        from app.schemas.instruction_schema import InstructionListSchema
        from app.schemas.data_source_schema import DataSourceMinimalSchema
        from app.schemas.instruction_label_schema import InstructionLabelSchema
        
        list_items: List[InstructionListSchema] = []
        for inst in instructions:
            ds_min = [DataSourceMinimalSchema.from_orm(ds) for ds in (inst.data_sources or [])]
            list_items.append(
                InstructionListSchema(
                    id=str(inst.id),
                    text=inst.text,
                    status=inst.status,
                    category=inst.category,
                    user_id=inst.user_id,
                    user=UserSchema.from_orm(inst.user) if inst.user else None,
                    organization_id=inst.organization_id,
                    private_status=inst.private_status,
                    global_status=inst.global_status,
                    is_seen=inst.is_seen,
                    can_user_toggle=inst.can_user_toggle,
                    reviewed_by_user_id=inst.reviewed_by_user_id,
                    reviewed_by=UserSchema.from_orm(inst.reviewed_by) if inst.reviewed_by else None,
                    data_sources=ds_min,
                    labels=[InstructionLabelSchema.from_orm(label) for label in (inst.labels or [])],
                    created_at=inst.created_at,
                    updated_at=inst.updated_at,
                    ai_source=getattr(inst, "ai_source", None),
                    # Unified Instructions System fields
                    source_type=getattr(inst, "source_type", "user") or "user",
                    source_metadata_resource_id=getattr(inst, "source_metadata_resource_id", None),
                    source_file_path=getattr(inst, "source_file_path", None),
                    source_git_commit_sha=getattr(inst, "source_git_commit_sha", None),
                    source_sync_enabled=getattr(inst, "source_sync_enabled", True) if getattr(inst, "source_sync_enabled", None) is not None else True,
                    load_mode=getattr(inst, "load_mode", "always") or "always",
                    kind=getattr(inst, "kind", "instruction") or "instruction",
                    title=getattr(inst, "title", None),
                    structured_data=getattr(inst, "structured_data", None),
                    formatted_content=getattr(inst, "formatted_content", None),
                    # Build System fields
                    current_version_id=getattr(inst, "current_version_id", None),
                )
            )
        
        # Batch-populate current_build_{id,status} so the list can show a
        # "Pending review" status. A build snapshots the whole instruction set,
        # so mere membership in a non-main draft/pending build is NOT a real
        # change — mirror GET /instructions/{id}/pending-builds and only count
        # builds whose instruction version DIFFERS from the main build's version
        # (or where the instruction isn't in main at all, i.e. created in a draft).
        if list_items:
            try:
                from app.models.instruction_build import InstructionBuild
                from app.models.build_content import BuildContent
                inst_ids = [it.id for it in list_items]
                # main build version per instruction
                main_rows = await db.execute(
                    select(BuildContent.instruction_id, BuildContent.instruction_version_id)
                    .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
                    .where(
                        BuildContent.instruction_id.in_(inst_ids),
                        InstructionBuild.organization_id == str(organization.id),
                        InstructionBuild.is_main == True,  # noqa: E712
                        InstructionBuild.deleted_at == None,  # noqa: E711
                        BuildContent.deleted_at == None,  # noqa: E711
                    )
                )
                main_ver: dict = {str(iid): vid for iid, vid in main_rows.all()}
                # non-main draft/pending builds, newest first
                build_rows = await db.execute(
                    select(
                        BuildContent.instruction_id,
                        InstructionBuild.id,
                        InstructionBuild.status,
                        BuildContent.instruction_version_id,
                    )
                    .join(InstructionBuild, BuildContent.build_id == InstructionBuild.id)
                    .where(
                        BuildContent.instruction_id.in_(inst_ids),
                        InstructionBuild.organization_id == str(organization.id),
                        InstructionBuild.is_main == False,  # noqa: E712
                        InstructionBuild.status.in_(['draft', 'pending_approval']),
                        InstructionBuild.deleted_at == None,  # noqa: E711
                        BuildContent.deleted_at == None,  # noqa: E711
                    )
                    .order_by(InstructionBuild.created_at.desc())
                )
                latest_by_inst: dict = {}
                for inst_id, b_id, b_status, ver_id in build_rows.all():
                    key = str(inst_id)
                    mv = main_ver.get(key)
                    if mv is not None and ver_id == mv:
                        continue  # inherited the main version — not a real pending change
                    if key not in latest_by_inst:  # rows are newest-first
                        latest_by_inst[key] = (str(b_id), b_status)
                for it in list_items:
                    hit = latest_by_inst.get(str(it.id))
                    if hit:
                        it.current_build_id, it.current_build_status = hit
            except Exception as e:
                logger.warning(f"Failed to batch-resolve current builds for instruction list: {e}")

        # Post-filter by per-user table accessibility (user_data_source_tables overlay).
        # Excludes instructions whose table references are ALL inaccessible to the user.
        if current_user:
            list_items = await self._filter_list_items_by_table_accessibility(
                db, list_items, str(current_user.id)
            )

        return {
            "items": list_items,
            "total": total,
            "page": (skip // limit) + 1 if limit > 0 else 1,
            "per_page": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 1
        }

    async def _filter_list_items_by_table_accessibility(
        self,
        db: AsyncSession,
        items: List,
        user_id: str,
    ) -> List:
        """Remove list items whose table references are all inaccessible to the user.

        Rules:
        - No table references → keep (global / text-only instruction)
        - All referenced tables inaccessible → exclude
        - At least one referenced table accessible → keep
        - No overlay rows for user → keep all (no filtering)
        """
        from app.models.user_data_source_overlay import UserDataSourceTable
        from app.models.instruction_reference import InstructionReference

        # Get the set of table IDs this user cannot access
        result = await db.execute(
            select(UserDataSourceTable.data_source_table_id)
            .where(
                UserDataSourceTable.user_id == user_id,
                UserDataSourceTable.is_accessible == False,
                UserDataSourceTable.data_source_table_id.isnot(None),
            )
        )
        inaccessible = {row[0] for row in result.all()}
        if not inaccessible:
            return items

        # Batch-load table references for all instruction IDs
        item_ids = [str(item.id) for item in items]
        if not item_ids:
            return items

        ref_result = await db.execute(
            select(InstructionReference.instruction_id, InstructionReference.object_id)
            .where(
                InstructionReference.instruction_id.in_(item_ids),
                InstructionReference.object_type == "datasource_table",
            )
        )
        refs_by_instruction: dict[str, set[str]] = {}
        for inst_id, table_id in ref_result.all():
            refs_by_instruction.setdefault(inst_id, set()).add(table_id)

        filtered = []
        for item in items:
            table_refs = refs_by_instruction.get(str(item.id))
            if not table_refs:
                filtered.append(item)
            elif table_refs - inaccessible:
                filtered.append(item)
            # else: all refs inaccessible → exclude
        return filtered

    async def _get_user_permissions(self, db: AsyncSession, user: User, organization: Organization) -> set:
        """Get user's org-level permissions via the RBAC resolver."""
        from app.core.permission_resolver import resolve_permissions

        resolved = await resolve_permissions(db, str(user.id), str(organization.id))
        return set(resolved.org_permissions)

    async def get_available_references(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        q: Optional[str] = None,
        types: Optional[str] = None,
        data_source_ids: Optional[str] = None,
    ) -> List[dict]:
        """Get available reference objects that user has access to - optimized version"""
        from sqlalchemy import union_all, literal
        from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
        
        wanted = set((types or "metadata_resource,datasource_table").split(","))
        items: List[dict] = []
        
        # Parse data_source_ids parameter if provided
        target_data_source_ids = None
        if data_source_ids:
            target_data_source_ids = [ds_id.strip() for ds_id in data_source_ids.split(",") if ds_id.strip()]
        
        # Build data source access subquery once
        from app.core.permission_resolver import get_accessible_data_source_ids
        _is_admin, _accessible_ids = await get_accessible_data_source_ids(
            db, str(current_user.id), str(organization.id)
        )
        if _is_admin:
            data_source_access_subquery = (
                select(DataSource.id).filter(DataSource.organization_id == organization.id)
            )
        else:
            _clauses = [DataSource.is_public == True]
            if _accessible_ids:
                _clauses.append(DataSource.id.in_(_accessible_ids))
            data_source_access_subquery = (
                select(DataSource.id)
                .filter(DataSource.organization_id == organization.id)
                .filter(or_(*_clauses))
            )
        
        # Apply data source filtering to subquery
        if target_data_source_ids:
            data_source_access_subquery = data_source_access_subquery.filter(
                DataSource.id.in_(target_data_source_ids)
            )
        
        queries_to_union = []
        
        # Metadata Resources query with data source info
        from app.models.connection import Connection
        from app.models.domain_connection import domain_connection
        
        if "metadata_resource" in wanted:
            mr_query = (
                select(
                    MetadataResource.id.label('id'),
                    literal('metadata_resource').label('type'),
                    MetadataResource.name.label('name'),
                    MetadataResource.data_source_id.label('data_source_id'),
                    DataSource.name.label('data_source_name'),
                    Connection.type.label('data_source_type'),
                    literal(None).label('text_preview')
                )
                .select_from(MetadataResource)
                .join(DataSource, MetadataResource.data_source_id == DataSource.id)
                .outerjoin(domain_connection, domain_connection.c.data_source_id == DataSource.id)
                .outerjoin(Connection, domain_connection.c.connection_id == Connection.id)
                .filter(MetadataResource.data_source_id.in_(data_source_access_subquery))
            )

            if q:
                mr_query = mr_query.filter(MetadataResource.name.ilike(f"%{q}%"))

            queries_to_union.append(mr_query)

        # DataSource Tables query with data source info
        if "datasource_table" in wanted:
            dt_query = (
                select(
                    DataSourceTable.id.label('id'),
                    literal('datasource_table').label('type'),
                    DataSourceTable.name.label('name'),
                    DataSourceTable.datasource_id.label('data_source_id'),
                    DataSource.name.label('data_source_name'),
                    Connection.type.label('data_source_type'),
                    literal(None).label('text_preview')
                )
                .select_from(DataSourceTable)
                .join(DataSource, DataSourceTable.datasource_id == DataSource.id)
                .outerjoin(domain_connection, domain_connection.c.data_source_id == DataSource.id)
                .outerjoin(Connection, domain_connection.c.connection_id == Connection.id)
                .filter(DataSourceTable.is_active == True)
                .filter(DataSourceTable.datasource_id.in_(data_source_access_subquery))
            )

            if q:
                dt_query = dt_query.filter(DataSourceTable.name.ilike(f"%{q}%"))

            queries_to_union.append(dt_query)

        # Instructions query (for @ mentions) - only published instructions in the main build
        if "instruction" in wanted:
            from sqlalchemy import func, case, exists
            from app.models.instruction import instruction_data_source_association
            from app.models.instruction_build import InstructionBuild
            from app.models.build_content import BuildContent

            # Build text_preview: first 50 chars + "..." if longer
            text_preview_expr = case(
                (func.length(Instruction.text) > 50, func.substr(Instruction.text, 1, 50) + '...'),
                else_=Instruction.text
            )

            # Only include instructions that exist in the main build
            main_build_instruction_ids = (
                select(BuildContent.instruction_id)
                .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
                .where(
                    and_(
                        InstructionBuild.organization_id == organization.id,
                        InstructionBuild.is_main == True,
                        InstructionBuild.deleted_at == None
                    )
                )
            )

            inst_query = (
                select(
                    Instruction.id.label('id'),
                    literal('instruction').label('type'),
                    Instruction.title.label('name'),
                    literal(None).label('data_source_id'),
                    literal(None).label('data_source_name'),
                    literal(None).label('data_source_type'),
                    text_preview_expr.label('text_preview')
                )
                .filter(
                    and_(
                        Instruction.organization_id == organization.id,
                        Instruction.deleted_at == None,
                        Instruction.status == 'published',
                        Instruction.id.in_(main_build_instruction_ids)
                    )
                )
            )

            # Filter by data sources if specified
            if target_data_source_ids:
                # Include instructions that either:
                # 1. Have no data sources (global/general instructions)
                # 2. Have at least one of the target data sources
                has_no_data_sources = ~exists(
                    select(instruction_data_source_association.c.instruction_id)
                    .where(instruction_data_source_association.c.instruction_id == Instruction.id)
                )
                has_target_data_source = Instruction.id.in_(
                    select(instruction_data_source_association.c.instruction_id)
                    .where(instruction_data_source_association.c.data_source_id.in_(target_data_source_ids))
                )
                inst_query = inst_query.filter(
                    or_(has_no_data_sources, has_target_data_source)
                )

            if q:
                search_term = f"%{q}%"
                inst_query = inst_query.filter(
                    or_(
                        Instruction.title.ilike(search_term),
                        Instruction.text.ilike(search_term)
                    )
                )

            queries_to_union.append(inst_query)

        # Execute single UNION query if we have queries to run
        if queries_to_union:
            if len(queries_to_union) == 1:
                final_query = queries_to_union[0]
            else:
                final_query = union_all(*queries_to_union)
            
            result = await db.execute(final_query)
            for row in result.fetchall():
                item = {
                    "id": row.id,
                    "type": row.type,
                    "name": row.name,
                    "data_source_id": row.data_source_id,
                    "data_source_name": row.data_source_name,
                    "data_source_type": row.data_source_type,
                    "text_preview": row.text_preview
                }

                items.append(item)

        # Connection tools — only when scoped to specific data sources.
        # Runs outside the UNION to apply per-agent overlay logic cleanly.
        if "connection_tool" in wanted and target_data_source_ids:
            from app.models.connection_tool import ConnectionTool
            from app.models.data_source_connection_tool import DataSourceConnectionTool

            ct_q = (
                select(
                    ConnectionTool.id.label('id'),
                    ConnectionTool.name.label('name'),
                    ConnectionTool.description.label('description'),
                    Connection.id.label('connection_id'),
                    Connection.name.label('connection_name'),
                    Connection.type.label('connection_type'),
                    DataSourceConnectionTool.is_enabled.label('overlay_is_enabled'),
                    ConnectionTool.is_enabled.label('default_is_enabled'),
                )
                .select_from(ConnectionTool)
                .join(Connection, ConnectionTool.connection_id == Connection.id)
                .join(domain_connection, domain_connection.c.connection_id == Connection.id)
                .outerjoin(
                    DataSourceConnectionTool,
                    and_(
                        DataSourceConnectionTool.connection_tool_id == ConnectionTool.id,
                        DataSourceConnectionTool.data_source_id == domain_connection.c.data_source_id,
                        DataSourceConnectionTool.deleted_at.is_(None),
                    ),
                )
                .where(
                    domain_connection.c.data_source_id.in_(target_data_source_ids),
                    Connection.organization_id == organization.id,
                    ConnectionTool.deleted_at.is_(None),
                )
            )

            if q:
                ct_q = ct_q.where(
                    or_(
                        ConnectionTool.name.ilike(f"%{q}%"),
                        ConnectionTool.description.ilike(f"%{q}%"),
                    )
                )

            seen_tool_ids: set = set()
            for row in (await db.execute(ct_q)).fetchall():
                effective_enabled = (
                    row.overlay_is_enabled
                    if row.overlay_is_enabled is not None
                    else row.default_is_enabled
                )
                if not effective_enabled or row.id in seen_tool_ids:
                    continue
                seen_tool_ids.add(row.id)
                items.append({
                    "id": str(row.id),
                    "type": "connection_tool",
                    "name": row.name,
                    "data_source_id": None,
                    "data_source_name": row.connection_name,
                    "data_source_type": row.connection_type,
                    "text_preview": row.description,
                })

        return items

    async def _get_accessible_data_source_ids(
        self, 
        db: AsyncSession, 
        current_user: User, 
        organization: Organization
    ) -> List[str]:
        """Get list of data source IDs that the user has access to"""
        from app.core.permission_resolver import get_accessible_data_source_ids
        is_admin, accessible_ids = await get_accessible_data_source_ids(
            db, str(current_user.id), str(organization.id)
        )
        query = select(DataSource.id).filter(DataSource.organization_id == organization.id)
        if not is_admin:
            clauses = [DataSource.is_public == True]
            if accessible_ids:
                clauses.append(DataSource.id.in_(accessible_ids))
            query = query.filter(or_(*clauses))
        result = await db.execute(query)
        return [row[0] for row in result.fetchall()]

    async def _build_data_source_context(
        self,
        db: AsyncSession,
        organization: Organization,
        data_source_ids: List[str],
    ) -> str:
        """Build a lightweight context string for selected data sources."""
        if not data_source_ids:
            return ""

        stmt = (
            select(DataSource)
            .where(
                and_(
                    DataSource.id.in_(data_source_ids),
                    DataSource.organization_id == organization.id,
                )
            )
        )
        result = await db.execute(stmt)
        data_sources = result.scalars().all()

        parts: list[str] = []
        for ds in data_sources:
            description = getattr(ds, "description", None) or ""
            parts.append(f"Data Source: {ds.name} - {description}".strip())
        return "\n".join(parts)
    
    async def _instruction_to_schema_with_references(self, db: AsyncSession, instruction) -> InstructionSchema:
        """Convert instruction to schema with populated references."""
        # Convert to basic schema
        instruction_dict = InstructionSchema.from_orm(instruction).model_dump()

        # Look up the latest non-main build (draft / pending_approval) that
        # contains this instruction, so the UI can show an "unpublished build"
        # warning and offer a "View changes" affordance. is_main builds are
        # the live/published state and should not trigger the warning.
        try:
            from app.models.instruction_build import InstructionBuild
            from app.models.build_content import BuildContent
            from app.models.instruction_version import InstructionVersion as _IV
            from app.services.suggestion_merge import covers as _sm_covers
            iid = instruction.id
            # Current main version of this instruction (id + text).
            main_row = (await db.execute(
                select(BuildContent.instruction_version_id, _IV.text)
                .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
                .join(_IV, _IV.id == BuildContent.instruction_version_id)
                .where(
                    BuildContent.instruction_id == iid,
                    InstructionBuild.is_main == True,  # noqa: E712
                    InstructionBuild.deleted_at == None,  # noqa: E711
                ).limit(1)
            )).first()
            main_vid = str(main_row[0]) if main_row else None
            main_txt = main_row[1] if main_row else None
            # Candidate pending builds (newest first) with version + base.
            cand = (await db.execute(
                select(InstructionBuild.id, InstructionBuild.status, InstructionBuild.base_build_id,
                       BuildContent.instruction_version_id, _IV.text)
                .join(BuildContent, BuildContent.build_id == InstructionBuild.id)
                .join(_IV, _IV.id == BuildContent.instruction_version_id)
                .where(
                    BuildContent.instruction_id == iid,
                    InstructionBuild.is_main == False,  # noqa: E712
                    InstructionBuild.status.in_(['draft', 'pending_approval']),
                    InstructionBuild.deleted_at == None,  # noqa: E711
                    BuildContent.deleted_at == None,  # noqa: E711
                ).order_by(InstructionBuild.created_at.desc())
            )).all()
            base_ids = [str(b) for (_i, _s, b, _v, _t) in cand if b]
            base_vmap = {}
            if base_ids:
                for b_id, v_id in (await db.execute(
                    select(BuildContent.build_id, BuildContent.instruction_version_id)
                    .where(BuildContent.build_id.in_(base_ids), BuildContent.instruction_id == iid)
                )).all():
                    base_vmap[str(b_id)] = str(v_id)
            # Pick the latest build that is a REAL, non-no-op change — the same
            # rule the Review feed uses, so the status doesn't get stuck on a
            # stale leftover build after the instruction was promoted. A stale
            # sibling (base behind current) still counts as pending: its intended
            # change is rebased onto current in the review, so we don't exclude
            # it on freshness here either.
            for bid, st, base, vid, vtext in cand:
                vid = str(vid)
                if base:
                    base_vid = base_vmap.get(str(base))
                    changed = True if base_vid is None else (base_vid != vid)
                else:
                    changed = (main_vid != vid)
                if not changed:
                    continue
                # No-op vs current: exact match, or current already contains the
                # whole suggestion (its text is a pure-insertion subset of main).
                if main_txt is not None and (
                    (vtext or '') == (main_txt or '')
                    or _sm_covers(vtext or '', main_txt or '')
                ):
                    continue
                instruction_dict["current_build_id"] = str(bid)
                instruction_dict["current_build_status"] = st
                break
        except Exception as e:
            logger.warning(f"Failed to resolve current build for instruction {instruction.id}: {e}")

        # Populate primary_for: data sources that have this instruction as their primary
        try:
            from app.models.data_source import DataSource as DataSourceModel
            from app.schemas.data_source_schema import DataSourceMinimalSchema
            primary_result = await db.execute(
                select(DataSourceModel).where(
                    DataSourceModel.primary_instruction_id == str(instruction.id),
                    DataSourceModel.deleted_at.is_(None),
                )
            )
            primary_ds = primary_result.scalars().all()
            instruction_dict["primary_for"] = [
                DataSourceMinimalSchema(id=str(ds.id), name=ds.name).model_dump()
                for ds in primary_ds
            ]
        except Exception as e:
            logger.warning(f"Failed to resolve primary_for for instruction {instruction.id}: {e}")

        # Populate the referenced objects for each reference
        if instruction.references:
            logger.debug(f"Populating {len(instruction.references)} references for instruction {instruction.id}")
            populated_references = []
            for ref in instruction.references:
                ref_data = ref.__dict__.copy()
                # Remove SQLAlchemy internal attributes
                ref_data = {k: v for k, v in ref_data.items() if not k.startswith('_')}
                
                # Fetch and add the referenced object
                referenced_obj = await self.reference_service._fetch_referenced_object(db, ref.object_type, ref.object_id)
                if referenced_obj:
                    if ref.object_type == "metadata_resource":
                        from app.schemas.metadata_resource_schema import MetadataResourceSchema
                        ref_data["object"] = MetadataResourceSchema.from_orm(referenced_obj).model_dump()
                        
                        # Add data source info for metadata resources
                        from app.models.connection import Connection
                        from app.models.domain_connection import domain_connection
                        ds_result = await db.execute(
                            select(DataSource.name, Connection.type)
                            .select_from(DataSource)
                            .outerjoin(domain_connection, domain_connection.c.data_source_id == DataSource.id)
                            .outerjoin(Connection, domain_connection.c.connection_id == Connection.id)
                            .where(DataSource.id == referenced_obj.data_source_id)
                        )
                        ds_info = ds_result.first()
                        if ds_info:
                            ref_data["data_source_name"] = ds_info.name
                            ref_data["data_source_type"] = ds_info.type
                            ref_data["data_source_id"] = referenced_obj.data_source_id
                            
                    elif ref.object_type == "datasource_table":
                        from app.schemas.datasource_table_schema import DataSourceTableSchema
                        ref_data["object"] = DataSourceTableSchema.from_orm(referenced_obj).model_dump()
                        
                        # Add data source info for datasource tables
                        from app.models.connection import Connection
                        from app.models.domain_connection import domain_connection
                        ds_result = await db.execute(
                            select(DataSource.name, Connection.type)
                            .select_from(DataSource)
                            .outerjoin(domain_connection, domain_connection.c.data_source_id == DataSource.id)
                            .outerjoin(Connection, domain_connection.c.connection_id == Connection.id)
                            .where(DataSource.id == referenced_obj.datasource_id)
                        )
                        ds_info = ds_result.first()
                        if ds_info:
                            ref_data["data_source_name"] = ds_info.name
                            ref_data["data_source_type"] = ds_info.type
                            ref_data["data_source_id"] = referenced_obj.datasource_id
                else:
                    logger.warning(f"Referenced object not found: type={ref.object_type}, id={ref.object_id}")
                
                # Always include the reference, even if the object couldn't be fetched
                populated_references.append(InstructionReferenceSchema(**ref_data))
            
            instruction_dict["references"] = populated_references
            logger.debug(f"Returning {len(populated_references)} populated references")
        else:
            logger.debug(f"No references found for instruction {instruction.id}")
        
        return InstructionSchema(**instruction_dict)
    
    async def _auto_finalize_build(
        self,
        db: AsyncSession,
        build,
        current_user: User,
        user_permissions: set,
        trigger_reliability: bool = True,
    ) -> bool:
        """
        Auto-finalize a build based on user permissions.

        - Admins: approve only (makes build ready, but doesn't auto-promote)
        - Non-admins: submit for approval only (admin must review)

        Note: Promoting to main requires explicit action (Publish/Deploy button)

        Returns True if finalization completed (or there was nothing to do),
        False if it failed (e.g. a concurrent transaction held the
        ``instruction_builds`` lock until ``lock_timeout`` fired). On failure
        the session is rolled back so the caller can keep using it — otherwise
        Postgres leaves the transaction in an aborted state and every
        subsequent statement (refresh / eager-load) blows up with an opaque
        500, which is the cascade behind the reported "stuck on loading" /
        "Could not refresh instance" failures.
        """
        from app.models.instruction_build import InstructionBuild

        try:
            # Only finalize if still in draft state
            if build.status != 'draft':
                return True

            # Submit the build for approval
            await self.build_service.submit_build(db, build.id)

            # Check if user is admin
            is_admin = self._is_admin_permissions(user_permissions)

            if is_admin:
                # Admin: auto-approve and auto-promote to main
                await self.build_service.approve_build(
                    db, build.id, approved_by_user_id=current_user.id
                )
                if not build.is_main:
                    await self.build_service.promote_build(db, build.id, trigger_reliability=trigger_reliability)
                    logger.info(f"Auto-approved and promoted build {build.id} to main")
                else:
                    logger.info(f"Auto-approved build {build.id} (already main)")
            else:
                # Non-admin: leave in pending_approval for admin review
                logger.info(f"Build {build.id} submitted for admin approval (non-admin user)")

            # Single commit for all deferred audit logs from submit/approve/promote
            await db.commit()

            # Surface non-admin / AI suggestions in the admin Review feed (one
            # item per changed instruction × attached agent). Never block.
            if not is_admin and getattr(build, "source", "user") in ("user", "ai"):
                try:
                    from app.services.review_producers import emit_instruction_suggestions_for_build
                    await emit_instruction_suggestions_for_build(
                        db, str(build.organization_id), build,
                        why="A non-admin proposed this instruction change — review and accept or run an eval.",
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"review: failed to emit suggestion for build {build.id}: {e}")

            # Self Learning: a user manually proposing an instruction change is a
            # suggestion-creation site — run each affected agent's policy. Scoped
            # to source=='user' so AI/knowledge-harness builds (handled at the
            # harness site) and report training-mode builds are NOT double- or
            # wrongly-triggered here.
            if getattr(build, "source", "user") == "user":
                try:
                    from app.services.agent_reliability_service import AgentReliabilityService
                    AgentReliabilityService().schedule_for_suggestion(
                        organization_id=str(build.organization_id),
                        build_id=str(build.id),
                        user_id=str(current_user.id) if current_user else None,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Self Learning schedule failed for build {build.id}: {e}")
            return True
        except Exception as e:
            logger.warning(f"Failed to auto-finalize build {build.id}: {e}")
            # Roll back so the session is usable again (a swallowed error would
            # otherwise leave an aborted Postgres transaction).
            try:
                await db.rollback()
            except Exception:
                pass
            # Don't fail the instruction operation here; the caller decides
            # whether a failed finalize is fatal (see create_instruction).
            return False
    
    async def _instructions_to_schema_with_references(self, db: AsyncSession, instructions) -> List[InstructionSchema]:
        """Convert multiple instructions to schemas with populated references."""
        result = []
        for instruction in instructions:
            schema = await self._instruction_to_schema_with_references(db, instruction)
            result.append(schema)
        return result
