from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Any, Optional, Tuple
from app.models.external_platform import ExternalPlatform
from app.models.external_user_mapping import ExternalUserMapping
from app.schemas.external_user_mapping_schema import ExternalUserMappingCreate
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
from app.services.platform_adapters.base_adapter import PlatformAdapter  # Add this import
from app.services.external_platform_service import ExternalPlatformService
from app.services.external_user_mapping_service import ExternalUserMappingService
from app.services.organization_service import OrganizationService
from app.services.completion_service import CompletionService
from app.services.data_source_service import DataSourceService
from app.settings.config import settings


class ExternalPlatformManager:
    """Manages external platform interactions"""
    
    def __init__(self):
        self.platform_service = ExternalPlatformService()
        self.mapping_service = ExternalUserMappingService()
        self.organization_service = OrganizationService()
        self.completion_service = CompletionService()
        self.data_source_service = DataSourceService()

    async def handle_incoming_message(
        self, 
        db: AsyncSession, 
        platform_type: str,
        organization_id: str,
        event_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle incoming message from external platform"""
        
        try:
            # Get platform
            platform = await self.platform_service.get_platform_by_type(
                db, organization_id, platform_type
            )
            if not platform or not platform.is_active:
                return {"success": False, "error": "Platform not found or inactive"}
            
            # Create adapter
            adapter = PlatformAdapterFactory.create_adapter(platform)
            
            # Process message
            processed_data = await adapter.process_incoming_message(event_data)
            

            # Get or create user mapping
            user_mapping = await self._get_or_create_user_mapping(
                db, platform, processed_data, adapter
            )

            if not user_mapping:
                return {"success": False, "error": "User mapping not found"}
            
            if not user_mapping.is_verified:
                # Send verification message
                await self._handle_unverified_user(
                    db, adapter, processed_data, user_mapping
                )
                return {"success": True, "action": "verification_sent"}
            
            # Process verified message
            return await self._process_verified_message(
                db, adapter, processed_data, user_mapping
            )
            
        except Exception as e:
            print(f"Error handling incoming message: {e}")
            return {"success": False, "error": str(e)}
    
    async def _get_or_create_user_mapping(
        self, 
        db: AsyncSession, 
        platform: ExternalPlatform,
        processed_data: Dict[str, Any],
        adapter: PlatformAdapter
    ) -> Optional[ExternalUserMapping]:
        """Get or create user mapping"""
        
        external_user_id = processed_data.get("external_user_id")
        if not external_user_id:
            return None
        
        # Try to find existing mapping
        mapping = await self.mapping_service.get_mapping_by_external_id(
            db, platform.organization_id, platform.platform_type, external_user_id
        )
        
        if mapping:
            return mapping
        
        # Create unverified mapping (no app_user_id yet)
        mapping_data = ExternalUserMappingCreate(
            platform_type=platform.platform_type,
            external_user_id=external_user_id,
            external_email=None,  # Will be filled after verification
            external_name=None,   # Will be filled after verification
            app_user_id=None,     # Will be filled after verification
            is_verified=False
        )
        
        # Get organization for the mapping service
        organization = await self.organization_service.get_organization(db, platform.organization_id, None)
        
        try:
            # Pass the platform ID to the create_mapping method
            mapping = await self.mapping_service.create_mapping(db, organization, mapping_data, platform.id)
            return mapping
        except Exception as e:
            print(f"Error creating mapping: {e}")
            return None
    
    async def _handle_unverified_user(
        self, 
        db: AsyncSession, 
        adapter: PlatformAdapter,
        processed_data: Dict[str, Any],
        user_mapping: ExternalUserMapping
    ):
        """Handle unverified user - send verification link"""
        # Get organization for the mapping service
        organization = await self.organization_service.get_organization(db, user_mapping.organization_id, None)
        
        # Generate verification token
        token = await self.mapping_service.generate_verification_token(
            db, user_mapping.id, organization
        )

        # Send verification message with link
        await adapter.send_verification_message(
            processed_data.get("channel_id"),
            None,  # No email needed
            token
        )

    async def _get_or_create_conversation_report(
        self,
        db: AsyncSession,
        organization: Any,
        user: Any,
        user_mapping: ExternalUserMapping,
        channel_type: str = None,
    ) -> Tuple[Any, bool]:
        """
        Get a report for a user if one from the platform exists from the last 24 hours,
        otherwise create a new one.

        For channel mentions (channel_type="channel"), only public data sources are used.
        For DMs (channel_type="im" or None), user's accessible data sources are used
        (public + private data sources the user has membership to).

        Returns a tuple of (report, created).
        """
        from app.models.report import Report
        from app.services.report_service import ReportService
        from app.schemas.report_schema import ReportCreate
        from sqlalchemy import select, and_
        import datetime

        report_service = ReportService()

        # Check for a report from the last 24 hours for the user from the given platform
        twenty_four_hours_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=24)

        # Find a recent report for this user from the same platform
        platform_name = user_mapping.platform_type.capitalize()

        # this will neer work, need to be re-written to use completion reports and by that to find the relevant one
        result = await db.execute(
            select(Report)
            .filter(
                and_(
                    Report.external_platform_id == user_mapping.platform_id,
                    Report.organization_id == organization.id,
                    Report.user_id == user.id,
                    Report.created_at >= twenty_four_hours_ago
                )
            )
            .order_by(Report.created_at.desc())
            .limit(1)
        )
        report = result.scalar_one_or_none()
        report = None

        if report:
            return report, False

        # If no recent report, create a new one
        # For channel mentions, only use public data sources (visible to everyone in the channel)
        # For DMs, use all data sources the user has access to (public + private with membership)
        if channel_type == "channel":
            data_sources = await self.data_source_service.get_public_data_sources(db, organization)
        else:
            # DM: user gets public + private data sources they have access to
            data_sources = await self.data_source_service.get_active_data_sources(db, organization, user)

        data_source_ids = [data_source.id for data_source in data_sources]
        report_create_data = ReportCreate(
            title=f"Chat with {user.name} via {platform_name}",
            data_sources=data_source_ids,
            external_platform_id=user_mapping.platform_id
        )

        report = await report_service.create_report(
            db=db,
            report_data=report_create_data,
            current_user=user,
            organization=organization
        )

        return report, True

    async def _find_report_by_thread_ts(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        thread_ts: str,
        platform_type: str = "slack",
        max_age_days: int = None,
    ) -> Optional[Any]:
        """
        Find an existing report by looking up completions with the given thread_ts.
        If max_age_days is set, only matches completions created within that window.
        Returns the report if found, None otherwise.
        """
        from app.models.completion import Completion
        from app.models.report import Report
        from sqlalchemy import select
        import datetime

        # Find the most recent completion with this thread_ts
        filters = [
            Completion.external_thread_ts == thread_ts,
            Completion.external_platform == platform_type,
        ]
        if max_age_days is not None:
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)
            filters.append(Completion.created_at >= cutoff)

        stmt = (
            select(Completion)
            .where(*filters)
            .order_by(Completion.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        completion = result.scalar_one_or_none()

        if completion:
            # Verify the report belongs to this org and user
            report_stmt = select(Report).where(
                Report.id == completion.report_id,
                Report.organization_id == organization_id,
                Report.user_id == user_id,
            )
            report_result = await db.execute(report_stmt)
            return report_result.scalar_one_or_none()

        return None

    async def _process_verified_message(
        self,
        db: AsyncSession,
        adapter: PlatformAdapter,
        processed_data: Dict[str, Any],
        user_mapping: ExternalUserMapping
    ) -> Dict[str, Any]:
        """Process message from verified user"""

        # Get user and organization
        user = await self.mapping_service.get_user_by_id(db, user_mapping.app_user_id)
        if not user:
            return {"success": False, "error": "User not found"}

        organization = await self.organization_service.get_organization(db, user_mapping.organization_id, None)
        if not organization:
            return {"success": False, "error": "Organization not found"}

        # Extract thread context from processed data
        thread_ts = processed_data.get("thread_ts")
        message_ts = processed_data.get("message_ts")
        channel_id = processed_data.get("channel_id")
        channel_type = processed_data.get("channel_type")
        is_thread_reply = processed_data.get("is_thread_reply", False)

        # Add eyes reaction to show we're processing
        if channel_id and message_ts:
            platform_label = processed_data.get("platform_type", "slack").upper()
            print(f"{platform_label}: Adding eyes reaction to channel={channel_id}, message_ts={message_ts}")
            result = await adapter.add_reaction(channel_id, message_ts, "eyes")
            print(f"{platform_label}: Eyes reaction result: {result}")

        # Determine report: Thread reply -> find existing, Top-level -> create new
        report = None
        created = False

        platform_type = processed_data.get("platform_type", "slack")

        if is_thread_reply and thread_ts:
            # This is a reply in an existing thread - find the associated report
            report = await self._find_report_by_thread_ts(
                db, organization.id, user.id, thread_ts,
                platform_type=platform_type,
            )
        elif platform_type == "teams" and channel_type == "personal" and thread_ts:
            # Teams 1:1 chats have no threading — reuse report by conversation ID
            # within a 5-day window so old conversations start fresh
            report = await self._find_report_by_thread_ts(
                db, organization.id, user.id, thread_ts,
                platform_type=platform_type,
                max_age_days=5,
            )

        if not report:
            # Create a new report (either top-level message or thread not found)
            # Pass channel_type to determine data source filtering:
            # - channel: only public data sources
            # - im/DM: public + user's private data sources
            report, created = await self._get_or_create_conversation_report(
                db, organization, user, user_mapping, channel_type
            )

            if created:
                report_url = f"{settings.bow_config.base_url}/reports/{report.id}"
                # Send the "new report" message in the thread
                # For channel mentions, respond in the channel; for DMs, open a DM
                # Slack DMs: None (adapter opens DM by user_id)
                # Teams: always use conversation ID (required for all Teams messages)
                if processed_data.get("platform_type") == "teams":
                    response_channel = channel_id
                else:
                    response_channel = channel_id if channel_type == "channel" else None
                # Format link based on platform (Slack uses <url|text>, Teams uses markdown)
                if processed_data.get("platform_type") == "teams":
                    report_link = f"[{report.title}]({report_url})"
                else:
                    report_link = f"<{report_url}|{report.title}>"

                await adapter.send_dm_in_thread(
                    user_mapping.external_user_id,
                    f"> I've started a new conversation report for you: {report_link}",
                    thread_ts,
                    channel_id=response_channel
                )

        # Create completion data
        from app.schemas.completion_schema import CompletionCreate, PromptSchema

        completion_data = CompletionCreate(
            prompt=PromptSchema(
                content=processed_data.get("message_text"),
                widget_id=None,
                step_id=None,
                mentions=[
                    {'name': 'MEMORY', 'items': []},
                    {'name': 'FILES', 'items': []},
                    {'name': 'DATA SOURCES', 'items': []}
                ]
            )
        )

        # Create completion with thread context (background=True to avoid blocking the webhook)
        await self.completion_service.create_completion(
            db=db,
            report_id=str(report.id),
            completion_data=completion_data,
            current_user=user,
            organization=organization,
            background=True,
            external_user_id=user_mapping.external_user_id,
            external_platform=user_mapping.platform_type,
            external_thread_ts=thread_ts,
            external_message_ts=message_ts,
            external_channel_id=channel_id,
            external_channel_type=channel_type
        )


        return {
            "success": True,
            "action": "message_processed",
            "user_id": user_mapping.app_user_id,
            "message": processed_data.get("message_text"),
            "thread_ts": thread_ts
        }
