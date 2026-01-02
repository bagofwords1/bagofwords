from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
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
from app.dependencies import async_session_maker
from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
from app.core.telemetry import telemetry
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
        
        # SIMPLIFIED: All instructions are "published" (content ready)
        # Approval workflow is handled by builds, not instruction status
        # - Non-admin: build stays in pending_approval for admin review
        # - Admin: build auto-approved and promoted to main
        instruction.status = instruction_data.status or "published"
        # Leave private_status and global_status as NULL (deprecated)
            
        db.add(instruction)
        await db.commit()
        await db.refresh(instruction)
        
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
        try:
            # Re-fetch instruction with relationships for version creation
            await db.refresh(instruction, ['data_sources', 'labels', 'references'])
            
            # Create the first version
            version = await self.version_service.create_version(
                db, instruction, user_id=current_user.id
            )
            
            # Update instruction's current version
            instruction.current_version_id = version.id
            
            # Use provided build or get/create a draft build for user changes
            target_build = build
            if target_build is None:
                target_build = await self.build_service.get_or_create_draft_build(
                    db, organization.id, source='user', user_id=current_user.id
                )
            
            # Add the version to the build
            await self.build_service.add_to_build(
                db, target_build.id, instruction.id, version.id
            )
            
            await db.commit()
            
            # Auto-finalize unless explicitly disabled (for batching scenarios)
            if auto_finalize:
                await self._auto_finalize_build(db, target_build, current_user, user_permissions)
            
            logger.info(f"Created version {version.id} for instruction {instruction.id}, added to build {target_build.id}")
        except Exception as e:
            logger.warning(f"Failed to create version for instruction {instruction.id}: {e}")
            # Don't fail the instruction creation if versioning fails
        
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

        # Load relationships and return
        await db.refresh(instruction)
        # Re-fetch instruction with proper eager loading to avoid lazy loading issues
        fresh_instruction = await db.execute(
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).selectinload(DataSource.data_source_memberships),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
                selectinload(Instruction.labels),
            )
            .where(Instruction.id == instruction.id)
        )
        instruction = fresh_instruction.scalar_one()
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
            related_instructions_result = RelatedInstructions(count=len(ranked), items=items)

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
            build_id=build_id, include_global=include_global
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
        dbt_count = await db.scalar(
            select(func.count(distinct(Instruction.id)))
            .select_from(Instruction)
            .outerjoin(MetadataResource, Instruction.source_metadata_resource_id == MetadataResource.id)
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None,
                    Instruction.source_type == 'git',
                    MetadataResource.resource_type.like('dbt_%')
                )
            )
        )
        if dbt_count and dbt_count > 0:
            available_types.append({
                'value': 'dbt',
                'label': 'dbt',
                'icon': '/icons/dbt.png'
            })
        
        # Check for markdown instructions (git + markdown_document resource type)
        markdown_count = await db.scalar(
            select(func.count(distinct(Instruction.id)))
            .select_from(Instruction)
            .outerjoin(MetadataResource, Instruction.source_metadata_resource_id == MetadataResource.id)
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None,
                    Instruction.source_type == 'git',
                    MetadataResource.resource_type == 'markdown_document'
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
        
        query = (
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).selectinload(DataSource.data_source_memberships),
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
        else:
            raise HTTPException(status_code=403, detail="Permission denied")
        
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
                        logger.info(f"Created version {version.id} for instruction {instruction.id}, added to existing build {target_build.id}")
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
                    
                    logger.info(f"Created version {version.id} for instruction {instruction.id}, added to build {build.id}")
        except Exception as e:
            logger.warning(f"Failed to create version for updated instruction {instruction.id}: {e}")
            # Don't fail the update if versioning fails
        
        # Re-fetch instruction with proper eager loading to avoid lazy loading issues
        fresh_instruction = await db.execute(
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources).selectinload(DataSource.data_source_memberships),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
                selectinload(Instruction.labels),
            )
            .where(Instruction.id == instruction.id)
        )
        instruction = fresh_instruction.scalar_one()
        return await self._instruction_to_schema_with_references(db, instruction)
    
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
        suggest_instructions = SuggestInstructions(
            model=small_model,
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
                logger.info(f"Auto-promoted delete build {build.id} to main")
            
            logger.info(f"Removed instruction {instruction_id} from build {build.id}")
        except Exception as e:
            logger.warning(f"Failed to update build for deleted instruction {instruction_id}: {e}")
            # Don't fail the deletion if build update fails
        
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
                logger.info(f"Finalized bulk update build {bulk_build.id} with {len(modified_instructions)} instructions")
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
                    logger.info(f"Auto-promoted bulk delete build {bulk_build.id} to main")
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
                selectinload(Instruction.data_sources).selectinload(DataSource.data_source_memberships),
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
        return [InstructionSchema.from_orm(instruction) for instruction in instructions]

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
        """Determine what type of update this is based on permissions and changes.
        
        SIMPLIFIED: No more suggestion workflow. Just admin vs owner edits.
        """
        is_admin = self._is_admin_permissions(user_permissions)
        is_owner = instruction.user_id == current_user.id
        
        # Admin editing any instruction
        if is_admin:
            return "admin_edit"
        
        # Owner editing their own instruction
        elif is_owner and user_permissions:
            return "owner_edit"
        
        # No permission
        else:
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
        
        # Owner can only edit text/category/toggles. Ignore any status changes silently.
        allowed_fields = ['text', 'category', 'is_seen', 'can_user_toggle']
        
        # Apply allowed changes only (ignore status/private/global fields if present)
        for field in allowed_fields:
            if hasattr(instruction_data, field) and getattr(instruction_data, field) is not None:
                setattr(instruction, field, getattr(instruction_data, field))

    def _is_admin_permissions(self, user_permissions: set) -> bool:
        """Check if permissions set corresponds to an admin in org"""
        return 'update_instructions' in user_permissions or 'create_instructions' in user_permissions or 'delete_instructions' in user_permissions

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
        
        if 'create_instructions' in permissions:
            # Admin: see everything with optional filters
            if not include_drafts:
                base.append(Instruction.status != "draft")
            if not include_archived:
                base.append(Instruction.status != "archived")
            if not include_hidden:
                base.append(Instruction.is_seen == True)
            return and_(*base)
        
        elif 'view_instructions' in permissions:
            # Regular user: only published, visible instructions
            # Note: Build system controls actual visibility (whether in main build)
            base.extend([
                Instruction.status == "published",
                Instruction.is_seen == True,
            ])
            return and_(*base)
        
        else:
            # No permission to see others' instructions
            return None

    def _can_filter_by_user(self, permissions: set) -> bool:
        """Check if user can filter by specific user ID"""
        return 'create_instructions' in permissions

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
        include_global: bool = True
    ) -> dict:
        """Execute the instructions query with given conditions. Returns paginated response.
        
        By default, loads instructions from the main build (is_main=True).
        Pass build_id to load from a specific build instead.
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
                            Instruction.source_metadata_resource.has(
                                MetadataResource.resource_type.like('dbt_%')
                            )
                        )
                    )
                elif st == 'markdown':
                    # Git instructions with markdown resource type
                    source_type_conditions.append(
                        and_(
                            Instruction.source_type == 'git',
                            Instruction.source_metadata_resource.has(
                                MetadataResource.resource_type == 'markdown_document'
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
        
        # Build the main query
        query = (
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources),
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
                    data_sources=ds_min,
                    labels=[InstructionLabelSchema.from_orm(label) for label in (inst.labels or [])],
                    created_at=inst.created_at,
                    updated_at=inst.updated_at,
                    ai_source=getattr(inst, "ai_source", None),
                    # Unified Instructions System fields
                    source_type=getattr(inst, "source_type", "user") or "user",
                    source_metadata_resource_id=getattr(inst, "source_metadata_resource_id", None),
                    source_git_commit_sha=getattr(inst, "source_git_commit_sha", None),
                    source_sync_enabled=getattr(inst, "source_sync_enabled", True) if getattr(inst, "source_sync_enabled", None) is not None else True,
                    load_mode=getattr(inst, "load_mode", "always") or "always",
                    title=getattr(inst, "title", None),
                    structured_data=getattr(inst, "structured_data", None),
                    # Build System fields
                    current_version_id=getattr(inst, "current_version_id", None),
                )
            )
        
        return {
            "items": list_items,
            "total": total,
            "page": (skip // limit) + 1 if limit > 0 else 1,
            "per_page": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 1
        }

    async def _get_user_permissions(self, db: AsyncSession, user: User, organization: Organization) -> set:
        """Get user's permissions in the organization"""
        from app.models.membership import Membership, ROLES_PERMISSIONS
        
        stmt = select(Membership).where(
            Membership.user_id == user.id,
            Membership.organization_id == organization.id
        )
        result = await db.execute(stmt)
        membership = result.scalar_one_or_none()
        
        return ROLES_PERMISSIONS.get(membership.role, set()) if membership else set()

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
        data_source_access_subquery = (
            select(DataSource.id)
            .filter(DataSource.organization_id == organization.id)
            .filter(
                or_(
                    DataSource.is_public == True,  # Public data sources
                    DataSource.id.in_(
                        select(DataSourceMembership.data_source_id)
                        .filter(
                            DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
                            DataSourceMembership.principal_id == current_user.id
                        )
                    )  # User has explicit membership
                )
            )
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
                    Connection.type.label('data_source_type')
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
                    Connection.type.label('data_source_type')
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
                    "data_source_type": row.data_source_type
                }
                
                items.append(item)
        
        return items

    async def _get_accessible_data_source_ids(
        self, 
        db: AsyncSession, 
        current_user: User, 
        organization: Organization
    ) -> List[str]:
        """Get list of data source IDs that the user has access to"""
        # Query for data sources the user has access to (same logic as data_source_service)
        query = (
            select(DataSource.id)
            .filter(DataSource.organization_id == organization.id)
            .filter(
                or_(
                    DataSource.is_public == True,  # Public data sources
                    DataSource.id.in_(
                        select(DataSourceMembership.data_source_id)
                        .filter(
                            DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
                            DataSourceMembership.principal_id == current_user.id
                        )
                    )  # User has explicit membership
                )
            )
        )
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
    ) -> None:
        """
        Auto-finalize a build based on user permissions.
        
        - Admins: approve only (makes build ready, but doesn't auto-promote)
        - Non-admins: submit for approval only (admin must review)
        
        Note: Promoting to main requires explicit action (Publish/Deploy button)
        """
        from app.models.instruction_build import InstructionBuild
        
        try:
            # Only finalize if still in draft state
            if build.status != 'draft':
                return
            
            # Submit the build for approval
            await self.build_service.submit_build(db, build.id)
            
            # Check if user is admin
            is_admin = self._is_admin_permissions(user_permissions)
            
            if is_admin:
                # Admin: auto-approve and auto-promote to main
                await self.build_service.approve_build(
                    db, build.id, approved_by_user_id=current_user.id
                )
                await db.refresh(build)
                if not build.is_main:
                    await self.build_service.promote_build(db, build.id)
                    logger.info(f"Auto-approved and promoted build {build.id} to main")
                else:
                    logger.info(f"Auto-approved build {build.id} (already main)")
            else:
                # Non-admin: leave in pending_approval for admin review
                logger.info(f"Build {build.id} submitted for admin approval (non-admin user)")
        except Exception as e:
            logger.warning(f"Failed to auto-finalize build {build.id}: {e}")
            # Don't fail the instruction operation if auto-finalize fails
    
    async def _instructions_to_schema_with_references(self, db: AsyncSession, instructions) -> List[InstructionSchema]:
        """Convert multiple instructions to schemas with populated references."""
        result = []
        for instruction in instructions:
            schema = await self._instruction_to_schema_with_references(db, instruction)
            result.append(schema)
        return result
