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


# Sending one of these words — and nothing else — starts a fresh conversation
# report instead of continuing the current one. Matched case-insensitively after
# trimming surrounding whitespace, so "new", "New" and "  new  " all qualify but
# "new report" / "retry new" do not. Only platforms that reuse a report across
# messages (Teams 1:1, WhatsApp) honour it; see _is_new_conversation_command.
NEW_REPORT_COMMANDS = {"new", "חדש"}
NEW_REPORT_COMMAND_PLATFORMS = {"teams", "whatsapp"}


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

        # Get organization for the mapping service
        organization = await self.organization_service.get_organization(db, platform.organization_id, None)

        # Optional: auto-link via verified platform email (per-integration opt-in).
        # Trust model: workspace/tenant email is treated like an IdP-vouched
        # identity. Lookup ladder:
        #   1. existing member of this org with matching email -> link
        #   2. existing user (any org) + open invite to this org -> attach + link
        #   3. no user, but open invite to this org -> create user, attach, link
        #   4. no user, but this org's signup_policy admits the email domain
        #      -> create user + membership(role=auto_invite_role), link
        #   5. otherwise -> block with an "ask your admin" DM
        from app.core.auth import auto_provision_user_for_org

        auto_link_enabled = bool((platform.platform_config or {}).get("auto_link_by_email"))
        auto_linked_user = None
        auto_linked_email = None
        auto_linked_name = None
        block_message = None

        # Email is intentionally conservative: the inbound address is only as
        # trustworthy as the DMARC/DKIM verdict the poller already enforced, so
        # we auto-link to an *existing* member but never auto-provision a new
        # account from an inbound email (unlike Slack/Teams, whose identity is
        # vouched by the workspace IdP).
        allow_auto_provision = platform.platform_type in ("slack", "teams")

        # Email identity (verify-first). The From address is spoofable, so we
        # only engage senders the org already knows. A matched member is either
        # auto-verified (if auto-link is explicitly enabled) or sent a one-time
        # verification link (the default); everyone else is ignored + audited.
        # (Invite / registration-link rungs for not-yet-members are a TODO —
        # see docs/design/email-identity-and-transport.md.)
        if platform.platform_type == "email":
            matched = await self.mapping_service.find_user_by_email(
                db, platform.organization_id, external_user_id
            )
            if not matched:
                try:
                    from app.ee.audit.service import audit_service
                    await audit_service.log(
                        db=db,
                        organization_id=platform.organization_id,
                        action="email.ignored_non_member",
                        user_id=None,
                        resource_type="email_integration",
                        resource_id=str(platform.id),
                        details={
                            "from_address": external_user_id,
                            "reason": "sender is not a linked organization member",
                        },
                    )
                except Exception as e:
                    print(f"Failed to audit ignored email: {e}")
                return None
            if auto_link_enabled:
                # Auto-verify: link immediately (DMARC-gated convenience).
                auto_linked_user = matched
                auto_linked_email = external_user_id
                auto_linked_name = getattr(matched, "name", None)
            # else: matched member with verify-first -> fall through to create an
            # unverified mapping; handle_incoming_message sends the verify link.

        if auto_link_enabled and platform.platform_type in ("slack", "teams"):
            try:
                user_info = await adapter.get_user_info(
                    external_user_id,
                    conversation_id=processed_data.get("channel_id"),
                )
                email = (user_info or {}).get("email")
                display_name = (user_info or {}).get("real_name") or (user_info or {}).get("name")
                if not email:
                    block_message = (
                        "I couldn't read your workspace email, so I can't link you "
                        "to a BOW account. Ask your admin to check the integration's "
                        "email permissions."
                    )
                else:
                    matched = await self.mapping_service.find_user_by_email(
                        db, platform.organization_id, email
                    )
                    if matched:
                        auto_linked_user = matched
                    elif allow_auto_provision:
                        provisioned = await auto_provision_user_for_org(
                            db, platform.organization_id, email, name=display_name
                        )
                        if provisioned:
                            auto_linked_user = provisioned
                        else:
                            block_message = (
                                f"Your email {email} isn't linked to a BOW account "
                                f"in this workspace. Ask your admin to invite you, "
                                f"then try again."
                            )
                    else:
                        block_message = (
                            f"Your email {email} isn't linked to a Bag of words "
                            f"account in this workspace. Ask your admin to invite "
                            f"you, then email again."
                        )
                    if auto_linked_user:
                        auto_linked_email = email
                        auto_linked_name = display_name
            except Exception as e:
                print(f"Auto-link by email failed, falling back to verification: {e}")

        if block_message:
            # Email from a non-member: ignore it (no reply — avoid backscatter to
            # possibly-spoofed senders) and record it in the audit trail.
            if platform.platform_type == "email":
                try:
                    from app.ee.audit.service import audit_service
                    await audit_service.log(
                        db=db,
                        organization_id=platform.organization_id,
                        action="email.ignored_non_member",
                        user_id=None,
                        resource_type="email_integration",
                        resource_id=str(platform.id),
                        details={
                            "from_address": external_user_id,
                            "reason": "sender is not a linked organization member",
                        },
                    )
                except Exception as e:
                    print(f"Failed to audit ignored email: {e}")
                return None
            try:
                await adapter.send_dm(external_user_id, block_message)
            except Exception as e:
                print(f"Failed to send block DM: {e}")
            return None

        mapping_data = ExternalUserMappingCreate(
            platform_type=platform.platform_type,
            external_user_id=external_user_id,
            external_email=auto_linked_email,
            external_name=auto_linked_name,
            app_user_id=auto_linked_user.id if auto_linked_user else None,
            is_verified=bool(auto_linked_user),
        )

        try:
            # Pass the platform ID to the create_mapping method
            mapping = await self.mapping_service.create_mapping(db, organization, mapping_data, platform.id)
            if auto_linked_user:
                # Refetch ORM row to stamp last_verified_at
                orm_mapping = await self.mapping_service.get_mapping_by_external_id(
                    db, platform.organization_id, platform.platform_type, external_user_id
                )
                if orm_mapping:
                    import datetime as _dt
                    orm_mapping.last_verified_at = _dt.datetime.utcnow()
                    await db.commit()
                try:
                    await adapter.send_dm(
                        external_user_id,
                        f"You've been auto-linked to BOW account {auto_linked_email}. "
                        f"If this isn't you, contact your admin.",
                    )
                except Exception as e:
                    print(f"Failed to send auto-link notice DM: {e}")
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

    @staticmethod
    def _is_new_conversation_command(platform_type: str, text: str) -> bool:
        """Return True when the whole message is a single "start fresh" keyword.

        Only the exact word (e.g. "new" / "חדש") counts — "new report" or
        "retry new" are ordinary prompts, not commands — and only on platforms
        that otherwise reuse a report across messages (Teams 1:1, WhatsApp),
        where the user has no other way to break out of the running report.
        """
        if platform_type not in NEW_REPORT_COMMAND_PLATFORMS:
            return False
        if not text:
            return False
        return text.strip().lower() in NEW_REPORT_COMMANDS

    async def _get_session_max_age_hours(
        self,
        db: AsyncSession,
        organization_id: str,
        platform_type: str,
    ) -> int:
        """Return the org-configured conversation reuse window, in hours.

        Reads ``{platform_type}_session_max_age_hours`` from the org's settings
        (editable on the Channels settings page), falling back to the schema
        default (Teams: 120h, WhatsApp: 24h) when unset or invalid.
        """
        from app.models.organization_settings import OrganizationSettings
        from app.schemas.organization_settings_schema import OrganizationSettingsConfig
        from sqlalchemy import select

        key = f"{platform_type}_session_max_age_hours"
        default = getattr(OrganizationSettingsConfig(), key)
        try:
            result = await db.execute(
                select(OrganizationSettings)
                .filter(OrganizationSettings.organization_id == organization_id)
            )
            org_settings = result.scalar_one_or_none()
            value = org_settings.get_config(key, default) if org_settings else default
            # Tolerate FeatureConfig-shaped storage ({'value': N, ...}).
            if isinstance(value, dict):
                value = value.get("value")
            hours = int(value)
        except Exception:
            # The reuse window is auxiliary — never let its lookup break
            # message handling.
            return default
        return hours if hours > 0 else default

    async def _find_recent_platform_report(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        platform_id: str,
        max_age_hours: int,
    ) -> Optional[Any]:
        """Return the user's most recent report for a platform within the window.

        Report-level (rather than completion-level) lookup, so a freshly created
        report with no completions yet — e.g. one started by a "new" command —
        is still picked up by the next message.
        """
        from app.models.report import Report
        from sqlalchemy import select, and_
        import datetime

        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=max_age_hours)
        result = await db.execute(
            select(Report)
            .filter(
                and_(
                    Report.external_platform_id == platform_id,
                    Report.organization_id == organization_id,
                    Report.user_id == user_id,
                    Report.created_at >= cutoff,
                )
            )
            .order_by(Report.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_or_create_conversation_report(
        self,
        db: AsyncSession,
        organization: Any,
        user: Any,
        user_mapping: ExternalUserMapping,
        channel_type: str = None,
        force_new: bool = False,
        max_age_hours: Optional[int] = None,
    ) -> Tuple[Any, bool]:
        """
        Get a report for a user if one from the platform exists within the reuse
        window (``max_age_hours``, WhatsApp only — defaults to 24h when not
        passed), otherwise create a new one.

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

        # Find a recent report for this user from the same platform
        platform_name = user_mapping.platform_type.capitalize()

        # WhatsApp has no native threading or a stable conversation id, so reuse
        # the user's most recent report within the org-configured window
        # (default 24h; mirrors Teams' per-conversation reuse). Other platforms
        # mint a fresh report per top-level message and rely on thread replies
        # for continuation.
        if user_mapping.platform_type == "whatsapp" and not force_new:
            reuse_cutoff = datetime.datetime.utcnow() - datetime.timedelta(
                hours=max_age_hours if max_age_hours else 24
            )
            result = await db.execute(
                select(Report)
                .filter(
                    and_(
                        Report.external_platform_id == user_mapping.platform_id,
                        Report.organization_id == organization.id,
                        Report.user_id == user.id,
                        Report.created_at >= reuse_cutoff
                    )
                )
                .order_by(Report.created_at.desc())
                .limit(1)
            )
            report = result.scalar_one_or_none()
            if report:
                return report, False

        # If no recent report, create a new one
        # For channel mentions, only use public data sources (visible to everyone in the channel)
        # For DMs, use all data sources the user has access to (public + private with membership)
        platform_type = user_mapping.platform_type
        if channel_type == "channel":
            data_sources = await self.data_source_service.get_public_data_sources(db, organization, channel=platform_type)
        else:
            # DM: user gets public + private data sources they have access to
            data_sources = await self.data_source_service.get_active_data_sources(db, organization, user, channel=platform_type)

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

        # Membership invariant: a verified external mapping is not a standing
        # grant. If the linked user has since been removed from the org, stop
        # honoring their messages — unverify the mapping (so the next message
        # re-runs the link/verify flow) and tell them to ask their admin.
        from app.core.permission_resolver import principal_belongs_to_org
        if not await principal_belongs_to_org(db, user, str(organization.id)):
            user_mapping.is_verified = False
            await db.commit()
            try:
                await adapter.send_dm(
                    user_mapping.external_user_id,
                    "Your access to this workspace has been revoked. "
                    "Ask your admin to re-invite you if this is a mistake.",
                )
            except Exception as e:
                print(f"Failed to send access-revoked DM: {e}")
            return {"success": False, "error": "User is not a member of this organization"}

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
        message_text = processed_data.get("message_text") or ""

        # A lone "new"/"חדש" message is a command to start a fresh report, not a
        # prompt — only on platforms that otherwise keep reusing one report
        # (Teams 1:1, WhatsApp), where the user has no other way out. Force-create
        # the report, confirm it, and stop: there's no question to answer yet, so
        # we don't create a completion. The next message reuses this fresh report.
        if self._is_new_conversation_command(platform_type, message_text):
            report, _ = await self._get_or_create_conversation_report(
                db, organization, user, user_mapping, channel_type, force_new=True
            )
            report_url = f"{settings.bow_config.base_url}/reports/{report.id}"
            if platform_type == "teams":
                report_link = f"[{report.title}]({report_url})"
            else:  # whatsapp
                report_link = f"{report.title} ({report_url})"
            await adapter.send_dm_in_thread(
                user_mapping.external_user_id,
                f"> I've started a new conversation report for you: {report_link}",
                thread_ts,
                channel_id=channel_id,
            )
            return {
                "success": True,
                "action": "new_conversation_started",
                "user_id": user_mapping.app_user_id,
                "report_id": str(report.id),
                "thread_ts": thread_ts,
            }

        if platform_type == "teams" and channel_type == "personal":
            # Teams 1:1 chats have no threading, so reuse the user's most recent
            # Teams report within the org-configured window (default 120h / 5
            # days; older ones start fresh). This is report-level rather than
            # keyed on a completion's thread_ts, so a report just started by a
            # "new" command — which has no completion yet — is still the one
            # this next message continues.
            report = await self._find_recent_platform_report(
                db, organization.id, user.id, user_mapping.platform_id,
                max_age_hours=await self._get_session_max_age_hours(
                    db, organization.id, "teams"
                ),
            )
        elif is_thread_reply and thread_ts and platform_type != "whatsapp":
            # This is a reply in an existing thread - find the associated report.
            # WhatsApp is excluded: its thread_ts is the (per-message) inbound id,
            # not a stable conversation key, so it reuses by 24h window instead.
            report = await self._find_report_by_thread_ts(
                db, organization.id, user.id, thread_ts,
                platform_type=platform_type,
            )

        if not report:
            # Create a new report (either top-level message or thread not found)
            # Pass channel_type to determine data source filtering:
            # - channel: only public data sources
            # - im/DM: public + user's private data sources
            # Only WhatsApp reuses by time window inside, so only it needs the
            # org-configured hours.
            wa_max_age_hours = (
                await self._get_session_max_age_hours(db, organization.id, "whatsapp")
                if platform_type == "whatsapp"
                else None
            )
            report, created = await self._get_or_create_conversation_report(
                db, organization, user, user_mapping, channel_type,
                max_age_hours=wa_max_age_hours,
            )

            if created and platform_type != "whatsapp":
                # WhatsApp intentionally skips the "new report" announcement — with
                # 24h report reuse it would only fire on the first message of a
                # window, and the URL adds noise to a 1:1 chat.
                report_url = f"{settings.bow_config.base_url}/reports/{report.id}"
                # Send the "new report" message in the thread
                # For channel mentions, respond in the channel; for DMs, open a DM
                # Slack DMs: None (adapter opens DM by user_id)
                # Teams: always use conversation ID (required for all Teams messages)
                if processed_data.get("platform_type") in ("teams", "email"):
                    # Teams requires the conversation id; email routes by the
                    # sender address (which is the channel_id for email).
                    response_channel = channel_id
                else:
                    response_channel = channel_id if channel_type == "channel" else None
                # Format link based on platform (Slack uses <url|text>, Teams uses
                # markdown, email is plain text).
                if processed_data.get("platform_type") == "teams":
                    report_link = f"[{report.title}]({report_url})"
                elif processed_data.get("platform_type") == "email":
                    report_link = f"{report.title} ({report_url})"
                else:
                    report_link = f"<{report_url}|{report.title}>"

                await adapter.send_dm_in_thread(
                    user_mapping.external_user_id,
                    f"> I've started a new conversation report for you: {report_link}",
                    thread_ts,
                    channel_id=response_channel
                )

        # Teams reuses a single report per 1:1 conversation for up to the
        # org-configured window (default 5 days), so
        # a reused report can outlive changes to the user's data-source access:
        # grants made after creation wouldn't appear, and revocations would still
        # be queryable from the stale snapshot. Re-sync the report's attached
        # sources to the user's current set on each message — for Teams and
        # WhatsApp, both of which reuse a report across messages (Slack DMs/
        # channels mint a fresh report per top-level message) and only when
        # reusing an existing report (a freshly created one is already current).
        if platform_type in ("teams", "whatsapp") and report is not None and not created:
            from app.services.report_service import ReportService
            if channel_type == "channel":
                fresh = await self.data_source_service.get_public_data_sources(db, organization, channel=platform_type)
            else:
                fresh = await self.data_source_service.get_active_data_sources(db, organization, user, channel=platform_type)
            fresh_ids = [str(ds.id) for ds in fresh]
            await ReportService().set_data_sources_for_report(
                db, report, fresh_ids, user, organization
            )
            await db.commit()

        # Ingest inbound email attachments as report files (within size limits)
        # so the agent can read them. They surface via report.files in context.
        message_text = processed_data.get("message_text") or ""
        attachments = processed_data.get("attachments") or []
        attachments_skipped = processed_data.get("attachments_skipped") or []
        saved_names = []
        if attachments and platform_type == "email":
            from app.services.file_service import FileService
            file_service = FileService()
            for att in attachments:
                try:
                    saved = await file_service.save_bytes_as_file(
                        db,
                        att.get("content"),
                        att.get("filename"),
                        att.get("content_type"),
                        user,
                        organization,
                        report_id=str(report.id),
                    )
                    saved_names.append(saved.filename)
                except Exception as e:
                    print(f"Failed to save email attachment {att.get('filename')}: {e}")

        # Note attached / skipped files in the prompt so the agent is aware.
        note_lines = []
        if saved_names:
            note_lines.append("Attached file(s): " + ", ".join(saved_names))
        if attachments_skipped:
            note_lines.append(
                f"(skipped {len(attachments_skipped)} attachment(s) over the size limit: "
                + ", ".join(s.get("filename", "?") for s in attachments_skipped) + ")"
            )
        if note_lines:
            message_text = (message_text + "\n\n" + "\n".join(note_lines)).strip()

        # Create completion data
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema

        completion_data = CompletionCreate(
            prompt=PromptSchema(
                content=message_text,
                widget_id=None,
                step_id=None,
                mentions=[
                    {'name': 'MEMORY', 'items': []},
                    {'name': 'FILES', 'items': []},
                    {'name': 'DATA SOURCES', 'items': []}
                ],
                platform=platform_type,
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
