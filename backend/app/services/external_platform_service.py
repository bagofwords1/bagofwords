from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete
from fastapi import HTTPException
from typing import Any, List, Optional, Set
import json

from app.models.external_platform import ExternalPlatform
from app.models.external_platform_agent import ExternalPlatformAgent
from app.models.organization import Organization
from app.models.user import User
from app.schemas.external_platform_schema import (
    ExternalPlatformCreate, 
    ExternalPlatformUpdate, 
    ExternalPlatformSchema,
    SlackConfig,
    TeamsConfig,
    EmailConfig
)
from app.core.telemetry import telemetry

class ExternalPlatformService:
    
    async def create_platform(
        self, 
        db: AsyncSession, 
        organization: Organization,
        platform_data: ExternalPlatformCreate,
        current_user: User
    ) -> ExternalPlatformSchema:
        """Create a new external platform for an organization"""
        
        # Check if platform type already exists for this organization
        existing_platform = await self.get_platform_by_type(
            db, organization.id, platform_data.platform_type
        )
        if existing_platform:
            raise HTTPException(
                status_code=400, 
                detail=f"{platform_data.platform_type} platform already exists for this organization"
            )
        
        # Create platform
        platform = ExternalPlatform(
            organization_id=organization.id,
            platform_type=platform_data.platform_type,
            platform_config=platform_data.platform_config,
            is_active=platform_data.is_active
        )
        
        db.add(platform)
        await db.commit()
        await db.refresh(platform)
        # Telemetry: external platform created
        try:
            await telemetry.capture(
                "external_platform_created",
                {
                    "platform_id": str(platform.id),
                    "platform_type": platform.platform_type,
                    "is_active": bool(platform.is_active),
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass
        
        return ExternalPlatformSchema.from_orm(platform)
    
    async def get_platforms(
        self, 
        db: AsyncSession, 
        organization: Organization
    ) -> List[ExternalPlatformSchema]:
        """Get all external platforms for an organization"""
        
        stmt = select(ExternalPlatform).where(
            ExternalPlatform.organization_id == organization.id
        )
        result = await db.execute(stmt)
        platforms = result.scalars().all()
        
        return [ExternalPlatformSchema.from_orm(platform) for platform in platforms]
    
    async def get_platform_by_id(
        self, 
        db: AsyncSession, 
        platform_id: str,
        organization: Organization
    ) -> ExternalPlatform:
        """Get a specific platform by ID"""
        
        stmt = select(ExternalPlatform).where(
            and_(
                ExternalPlatform.id == platform_id,
                ExternalPlatform.organization_id == organization.id
            )
        )
        result = await db.execute(stmt)
        platform = result.scalar_one_or_none()
        
        if not platform:
            raise HTTPException(status_code=404, detail="External platform not found")
        
        return platform
    
    async def get_platform_by_type(
        self,
        db: AsyncSession,
        organization_id: str,
        platform_type: str
    ) -> Optional[ExternalPlatform]:
        """Get a single platform of this type for an organization.

        An organization may now hold several platforms of the same type (e.g.
        multiple WhatsApp numbers). This helper keeps the single-instance
        contract that outbound paths rely on (notifications, MCP, Excel): it
        returns the oldest matching platform and never raises when more than
        one exists. Inbound dispatch resolves the *specific* instance by its
        payload key instead — see ``get_platforms_by_type`` and the webhooks.
        """

        stmt = (
            select(ExternalPlatform)
            .where(
                and_(
                    ExternalPlatform.organization_id == organization_id,
                    ExternalPlatform.platform_type == platform_type
                )
            )
            .order_by(ExternalPlatform.created_at.asc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_platform_by_id_raw(
        self,
        db: AsyncSession,
        platform_id: str,
        organization_id: str
    ) -> Optional[ExternalPlatform]:
        """Fetch one platform ORM row by id, scoped to the org; None if absent.

        Unlike ``get_platform_by_id`` this takes the org id as a string and
        returns ``None`` instead of raising, so inbound dispatch can resolve
        the instance the webhook matched without turning a miss into a 404.
        """

        stmt = select(ExternalPlatform).where(
            and_(
                ExternalPlatform.id == platform_id,
                ExternalPlatform.organization_id == organization_id
            )
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_platforms_by_type(
        self,
        db: AsyncSession,
        organization_id: str,
        platform_type: str
    ) -> List[ExternalPlatform]:
        """Get every platform of this type for an organization (oldest first).

        Used wherever multiple instances of one channel type must be
        considered together — listing WhatsApp numbers, or matching an inbound
        event against the right instance by its payload key.
        """

        stmt = (
            select(ExternalPlatform)
            .where(
                and_(
                    ExternalPlatform.organization_id == organization_id,
                    ExternalPlatform.platform_type == platform_type
                )
            )
            .order_by(ExternalPlatform.created_at.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Per-instance agent scoping
    # ------------------------------------------------------------------

    async def get_scope_agent_ids(
        self,
        db: AsyncSession,
        platform_id: str,
        external_channel_id: str = "",
    ) -> Set[str]:
        """Return the agent (data source) ids a scope row set is scoped to.

        ``external_channel_id`` selects which rows: ``""`` (the default) reads
        the connection-default / per-instance scope; a real channel id reads
        that one channel's scope. An empty result means "not scoped" for that
        key — expose everything the messaging user can access (pre-existing
        behavior).
        """

        stmt = select(ExternalPlatformAgent.data_source_id).where(
            and_(
                ExternalPlatformAgent.platform_id == platform_id,
                ExternalPlatformAgent.external_channel_id == (external_channel_id or ""),
            )
        )
        result = await db.execute(stmt)
        return {str(row) for row in result.scalars().all()}

    async def resolve_scope_agent_ids(
        self,
        db: AsyncSession,
        platform_id: str,
        routing_channel_id: Optional[str] = None,
    ) -> Set[str]:
        """Resolve the effective agent scope for an incoming message.

        Most-specific wins: if ``routing_channel_id`` is given and that channel
        has its own scope rows, use them; otherwise fall back to the ``""``
        connection-default scope; an empty result means unscoped (expose
        everything the user can access). The scope only ever *narrows* the
        caller's list — it is intersected with the user's own access downstream.
        """

        if routing_channel_id:
            channel_scope = await self.get_scope_agent_ids(
                db, platform_id, routing_channel_id
            )
            if channel_scope:
                return channel_scope
        return await self.get_scope_agent_ids(db, platform_id, "")

    async def filter_org_data_source_ids(
        self, db: AsyncSession, organization_id: str, data_source_ids: List[str]
    ) -> List[str]:
        """Keep only ids that are data sources of this organization.

        Preserves the caller's order and de-duplicates. Used to validate an
        agent-scope request so cross-org or stale ids never reach the table.
        """

        from app.models.data_source import DataSource

        wanted = [str(d) for d in (data_source_ids or [])]
        if not wanted:
            return []
        stmt = select(DataSource.id).where(
            and_(
                DataSource.organization_id == organization_id,
                DataSource.id.in_(wanted),
            )
        )
        result = await db.execute(stmt)
        valid = {str(row) for row in result.scalars().all()}
        seen: Set[str] = set()
        ordered: List[str] = []
        for d in wanted:
            if d in valid and d not in seen:
                seen.add(d)
                ordered.append(d)
        return ordered

    async def set_scope_agents(
        self,
        db: AsyncSession,
        platform: ExternalPlatform,
        data_source_ids: List[str],
        external_channel_id: str = "",
    ) -> Set[str]:
        """Replace one scope key's agent set with ``data_source_ids``.

        ``external_channel_id`` picks the key: ``""`` (the default) sets the
        connection-default / per-instance scope; a real channel id sets that one
        channel's scope. Passing an empty list clears that key (back to exposing
        everything the user can access *for that key*). Ids are de-duplicated;
        this does not widen access — resolution still intersects with the
        messaging user's own agents.
        """

        channel_key = external_channel_id or ""
        await db.execute(
            delete(ExternalPlatformAgent).where(
                and_(
                    ExternalPlatformAgent.platform_id == platform.id,
                    ExternalPlatformAgent.external_channel_id == channel_key,
                )
            )
        )
        wanted = []
        seen: Set[str] = set()
        for ds_id in data_source_ids or []:
            ds_id = str(ds_id)
            if ds_id in seen:
                continue
            seen.add(ds_id)
            wanted.append(ds_id)
            db.add(
                ExternalPlatformAgent(
                    organization_id=platform.organization_id,
                    platform_id=platform.id,
                    external_channel_id=channel_key,
                    data_source_id=ds_id,
                )
            )
        await db.commit()
        return set(wanted)

    async def get_scope_map(
        self, db: AsyncSession, platform_id: str
    ) -> dict:
        """Return ``{external_channel_id: {agent_id, ...}}`` for every scope row.

        ``""`` is the connection-default key. Lets the settings UI render the
        default plus every channel's scope from a single read.
        """

        stmt = select(
            ExternalPlatformAgent.external_channel_id,
            ExternalPlatformAgent.data_source_id,
        ).where(ExternalPlatformAgent.platform_id == platform_id)
        rows = (await db.execute(stmt)).all()
        out: dict = {}
        for channel, ds in rows:
            out.setdefault(channel or "", set()).add(str(ds))
        return out

    # ------------------------------------------------------------------
    # Discovered channels (discover-on-traffic)
    # ------------------------------------------------------------------

    async def record_seen_channel(
        self,
        db: AsyncSession,
        organization_id: str,
        platform_id: str,
        external_channel_id: str,
        name: Optional[str] = None,
        channel_type: Optional[str] = None,
        overwrite_name: bool = False,
        name_fetcher: Optional[Any] = None,
    ):
        """Upsert a channel the bot just saw (or an admin is adding by hand).

        Fills in ``name``/``channel_type`` only when still empty, unless
        ``overwrite_name`` is set (the admin relabel path). Refreshes
        ``last_seen_at``. Returns the normalized channel id, or None for an
        empty id. Caller should treat failures as best-effort — discovery must
        never break message handling.

        ``name_fetcher`` is an optional ``async (channel_id) -> Optional[str]``
        used to resolve a human name when none is known yet (Slack events carry
        only the id). It is awaited at most once per channel — only when no name
        was passed and none is stored — so the live lookup never repeats on
        every message. Any failure is swallowed and the id stays the label.
        """
        from app.models.external_platform_channel import ExternalPlatformChannel
        import datetime

        channel_id = (external_channel_id or "").strip()
        if not channel_id:
            return None

        existing = (
            await db.execute(
                select(ExternalPlatformChannel).where(
                    and_(
                        ExternalPlatformChannel.platform_id == platform_id,
                        ExternalPlatformChannel.external_channel_id == channel_id,
                    )
                )
            )
        ).scalar_one_or_none()

        # Resolve a name once, on first sighting: ask Slack only when we have
        # no name from the caller and none on record. Best-effort — a missing
        # scope or API hiccup just leaves the id as the label.
        if name_fetcher is not None and not name and not (existing and existing.name):
            try:
                name = await name_fetcher(channel_id)
            except Exception as e:
                print(f"record_seen_channel name_fetcher failed (non-fatal): {e}")

        now = datetime.datetime.utcnow()
        if existing:
            existing.last_seen_at = now
            if name and (overwrite_name or not existing.name):
                existing.name = name
            if channel_type and not existing.channel_type:
                existing.channel_type = channel_type
        else:
            db.add(
                ExternalPlatformChannel(
                    organization_id=organization_id,
                    platform_id=platform_id,
                    external_channel_id=channel_id,
                    name=name,
                    channel_type=channel_type,
                    last_seen_at=now,
                )
            )
        await db.commit()
        return channel_id

    async def list_channels(self, db: AsyncSession, platform_id: str) -> List:
        """All channels discovered on an instance, most-recently-seen first."""
        from app.models.external_platform_channel import ExternalPlatformChannel

        stmt = (
            select(ExternalPlatformChannel)
            .where(ExternalPlatformChannel.platform_id == platform_id)
            .order_by(ExternalPlatformChannel.last_seen_at.desc())
        )
        return list((await db.execute(stmt)).scalars().all())

    async def update_platform(
        self, 
        db: AsyncSession, 
        platform_id: str,
        platform_data: ExternalPlatformUpdate,
        organization: Organization
    ) -> ExternalPlatformSchema:
        """Update an external platform"""
        
        platform = await self.get_platform_by_id(db, platform_id, organization)
        
        # Update fields
        if platform_data.platform_config is not None:
            platform.platform_config = platform_data.platform_config
        if platform_data.is_active is not None:
            platform.is_active = platform_data.is_active
        
        await db.commit()
        await db.refresh(platform)
        
        return ExternalPlatformSchema.from_orm(platform)
    
    async def delete_platform(
        self,
        db: AsyncSession,
        platform_id: str,
        organization: Organization
    ) -> bool:
        """Delete an external platform"""

        platform = await self.get_platform_by_id(db, platform_id, organization)

        # Delete associated user mappings first (NOT NULL FK constraint)
        from app.models.external_user_mapping import ExternalUserMapping
        mapping_stmt = select(ExternalUserMapping).where(
            ExternalUserMapping.platform_id == str(platform.id)
        )
        mapping_result = await db.execute(mapping_stmt)
        for mapping in mapping_result.scalars().all():
            await db.delete(mapping)

        platform_type = platform.platform_type
        await db.delete(platform)
        await db.commit()
        # Telemetry: external platform deleted
        try:
            await telemetry.capture(
                "external_platform_deleted",
                {
                    "platform_type": platform_type,
                },
                user_id=None,
                org_id=organization.id,
            )
        except Exception:
            pass

        return True
    
    async def test_platform_connection(
        self, 
        db: AsyncSession, 
        platform_id: str,
        organization: Organization
    ) -> dict:
        """Test the connection to an external platform"""
        
        platform = await self.get_platform_by_id(db, platform_id, organization)
        
        try:
            if platform.platform_type == "slack":
                return await self._test_slack_connection(platform)
            elif platform.platform_type == "teams":
                return await self._test_teams_connection(platform)
            elif platform.platform_type == "whatsapp":
                return await self._test_whatsapp_connection(platform)
            elif platform.platform_type == "email":
                return await self._test_email_connection(platform)
            elif platform.platform_type == "google_chat":
                return await self._test_google_chat_connection(platform)
            else:
                raise HTTPException(status_code=400, detail="Unsupported platform type")
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _test_slack_connection(self, platform: ExternalPlatform) -> dict:
        """Test Slack connection"""
        try:
            import httpx
            
            # Extract bot token from config
            config = platform.platform_config
            bot_token = config.get("bot_token")
            
            if not bot_token:
                return {"success": False, "error": "Bot token not configured"}
            
            # Test API call
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        return {
                            "success": True, 
                            "workspace": data.get("team"),
                            "bot_user": data.get("user")
                        }
                    else:
                        return {"success": False, "error": data.get("error", "Unknown error")}
                else:
                    return {"success": False, "error": f"HTTP {response.status_code}"}
                    
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _test_teams_connection(self, platform: ExternalPlatform) -> dict:
        """Test Teams connection by acquiring an OAuth2 token"""
        try:
            import httpx

            credentials = platform.decrypt_credentials()
            app_id = credentials.get("app_id")
            client_secret = credentials.get("client_secret")

            if not app_id or not client_secret:
                return {"success": False, "error": "Missing app_id or client_secret"}

            tenant_id = platform.platform_config.get("tenant_id", "botframework.com")
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": app_id,
                        "client_secret": client_secret,
                        "scope": "https://api.botframework.com/.default",
                    },
                )
                if response.status_code == 200 and response.json().get("access_token"):
                    return {"success": True, "app_id": app_id}
                else:
                    error = response.json().get("error_description", "Authentication failed")
                    return {"success": False, "error": error}

        except Exception as e:
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def _email_provider_defaults(auth_type: str) -> dict:
        """Default SMTP/IMAP hosts for the OAuth providers (app-only + delegated)."""
        if auth_type in ("microsoft", "microsoft_delegated"):
            return {
                "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls",
                "imap_host": "outlook.office365.com", "imap_port": 993, "imap_use_ssl": True,
            }
        if auth_type in ("google", "google_delegated"):
            return {
                "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_security": "starttls",
                "imap_host": "imap.gmail.com", "imap_port": 993, "imap_use_ssl": True,
            }
        return {}

    async def _test_email_connection(self, platform: ExternalPlatform) -> dict:
        """Test Email connection: verify SMTP, and IMAP if inbound is configured."""
        creds = platform.decrypt_credentials()
        cfg = platform.platform_config or {}
        return await self._test_email_credentials(creds, cfg)

    async def _test_email_credentials(self, creds: dict, cfg: dict) -> dict:
        """Validate SMTP (always) and IMAP (if inbound) connectivity.

        For OAuth auth types this mints a real token and authenticates with
        XOAUTH2 (SMTP AUTH XOAUTH2 / IMAP AUTHENTICATE XOAUTH2) without sending.
        """
        import asyncio
        from app.services.email.pinned import PinnedIMAP4, PinnedIMAP4_SSL, PinnedSMTP
        from app.services.email.sender import SmtpConfig
        from app.services.email.mailbox_reader import ImapConfig
        from app.services.email.oauth import (
            OAuthSettings, get_access_token, build_xoauth2, build_xoauth2_raw,
        )

        result = {"success": True, "smtp": None, "imap": None}
        auth_type = creds.get("auth_type") or cfg.get("auth_type") or "password"
        oauth = OAuthSettings.from_credentials(creds, cfg)

        # Mint the token once (reused for SMTP + IMAP). SMTP wants the base64
        # SASL; imaplib base64-encodes itself, so IMAP needs the raw string.
        sasl = None
        sasl_raw = None
        if oauth is not None:
            try:
                token = await get_access_token(oauth)
                sasl = build_xoauth2(oauth.mailbox, token)
                sasl_raw = build_xoauth2_raw(oauth.mailbox, token)
            except Exception as e:
                return {"success": False, "smtp": f"oauth token failed: {e}", "imap": None}

        # SMTP probe. Negotiate TLS via start_tls in the constructor (a manual
        # starttls() after connect double-negotiates -> "already using TLS").
        try:
            smtp = SmtpConfig.from_credentials(creds, cfg).resolved()
            client = PinnedSMTP(
                hostname=smtp.host, port=smtp.port,
                use_tls=(smtp.security == "ssl"),
                start_tls=(smtp.security == "starttls"),
                timeout=15,
            )
            await client.connect()
            if sasl is not None:
                await client.ehlo()  # re-EHLO before low-level AUTH (503 otherwise)
                code, msg = await client.execute_command(b"AUTH", b"XOAUTH2", sasl.encode("ascii"))
                if code != 235:
                    return {"success": False, "smtp": f"xoauth2 rejected ({code})", "imap": None}
            elif smtp.auth_type == "password" and smtp.username and smtp.password:
                await client.login(smtp.username, smtp.password)
            await client.quit()
            result["smtp"] = "ok"
        except Exception as e:
            return {"success": False, "smtp": f"failed: {e}", "imap": None}

        # IMAP probe (only if inbound configured).
        imap = ImapConfig.from_credentials(creds, cfg)
        if imap.host and (cfg.get("inbound_enabled") or auth_type == "password"):
            try:
                def _probe():
                    conn = (
                        PinnedIMAP4_SSL(imap.host, imap.port)
                        if imap.use_ssl else PinnedIMAP4(imap.host, imap.port)
                    )
                    if sasl_raw is not None:
                        conn.authenticate("XOAUTH2", lambda _c: sasl_raw.encode("ascii"))
                    else:
                        conn.login(imap.username, imap.password)
                    conn.select(imap.mailbox)
                    conn.logout()

                await asyncio.to_thread(_probe)
                result["imap"] = "ok"
            except Exception as e:
                return {"success": False, "smtp": "ok", "imap": f"failed: {e}"}

        return result

    def _email_creds_and_config(self, data) -> tuple:
        """Build (platform_config, credentials) from an EmailConfig payload.

        Shared by create + test so both apply the same provider host defaults,
        capability derivation, and secret layout.
        """
        auth_type = data.auth_type or "password"
        defaults = self._email_provider_defaults(auth_type)
        from_address = data.from_address or data.smtp_username or data.imap_username

        smtp_host = data.smtp_host or defaults.get("smtp_host")
        smtp_port = data.smtp_port or defaults.get("smtp_port") or 587
        smtp_security = data.smtp_security or defaults.get("smtp_security") or "starttls"
        imap_host = data.imap_host or defaults.get("imap_host")
        imap_port = data.imap_port or defaults.get("imap_port") or 993
        imap_use_ssl = data.imap_use_ssl if data.imap_use_ssl is not None else True

        smtp_username = data.smtp_username or (from_address if auth_type != "password" else None)
        imap_username = data.imap_username or (from_address if auth_type != "password" else None)

        if auth_type == "password":
            inbound_enabled = bool(imap_host and imap_username) or bool(data.inbound_enabled)
        else:
            inbound_enabled = bool(data.inbound_enabled)

        google_sa = data.google_service_account_json
        if isinstance(google_sa, str) and google_sa.strip():
            try:
                google_sa = json.loads(google_sa)
            except Exception:
                raise HTTPException(status_code=400, detail="google_service_account_json is not valid JSON")

        platform_config = {
            "from_address": from_address,
            "from_name": data.from_name,
            "auth_type": auth_type,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_security": smtp_security,
            "imap_host": imap_host if inbound_enabled else None,
            "imap_port": imap_port,
            "imap_use_ssl": imap_use_ssl,
            "imap_mailbox": data.imap_mailbox,
            "inbound_enabled": inbound_enabled,
            "allowed_domains": data.allowed_domains,
            "auto_link_by_email": data.auto_link_by_email,
            "require_auth_pass": data.require_auth_pass,
            "ms_tenant_id": data.ms_tenant_id,
            "ms_client_id": data.ms_client_id,
            "capabilities": ["send", "receive"] if inbound_enabled else ["send"],
        }
        credentials = {
            "auth_type": auth_type,
            "from_address": from_address,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_security": smtp_security,
            "smtp_username": smtp_username,
            "smtp_password": data.smtp_password,
            "imap_host": imap_host,
            "imap_port": imap_port,
            "imap_username": imap_username,
            "imap_password": data.imap_password,
            "ms_tenant_id": data.ms_tenant_id,
            "ms_client_id": data.ms_client_id,
            "ms_client_secret": data.ms_client_secret,
            "google_service_account_info": google_sa,
            "ms_refresh_token": data.ms_refresh_token,
            "google_client_id": data.google_client_id,
            "google_client_secret": data.google_client_secret,
            "google_refresh_token": data.google_refresh_token,
        }
        return platform_config, credentials

    async def test_email_config(self, data) -> dict:
        """Validate an EmailConfig payload's connectivity WITHOUT persisting it.

        Backs the form's "Test connection" button: mints a token (for OAuth),
        authenticates SMTP (+IMAP if inbound), and returns the per-protocol
        result — nothing is saved.
        """
        platform_config, credentials = self._email_creds_and_config(data)
        return await self._test_email_credentials(credentials, platform_config)

    async def create_email_platform(
        self,
        db: AsyncSession,
        organization: Organization,
        data,
        current_user: User,
    ) -> ExternalPlatformSchema:
        """Create an Email integration.

        SMTP-only -> outbound transport (SEND). Adding IMAP -> conversational
        channel (SEND + RECEIVE). Connectivity is validated before saving.
        """
        existing_platform = await self.get_platform_by_type(db, organization.id, "email")
        if existing_platform:
            raise HTTPException(
                status_code=400,
                detail="Email integration already exists for this organization",
            )

        platform_config, credentials = self._email_creds_and_config(data)

        # Validate connectivity before persisting.
        test = await self._test_email_credentials(credentials, platform_config)
        if not test.get("success"):
            detail = test.get("smtp") if test.get("smtp") not in (None, "ok") else test.get("imap")
            raise HTTPException(status_code=400, detail=f"Email connection failed: {detail}")

        platform = ExternalPlatform(
            organization_id=organization.id,
            platform_type="email",
            platform_config=platform_config,
            is_active=True,
        )
        platform.encrypt_credentials(credentials)

        db.add(platform)
        await db.commit()
        await db.refresh(platform)

        try:
            await telemetry.capture(
                "external_platform_created",
                {
                    "platform_id": str(platform.id),
                    "platform_type": "email",
                    "is_active": True,
                    "inbound_enabled": inbound_enabled,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        return ExternalPlatformSchema.from_orm(platform)

    async def create_slack_platform(
        self,
        db: AsyncSession,
        organization: Organization,
        bot_token: str,
        signing_secret: Optional[str],
        current_user: User,
        auto_link_by_email: bool = True,
        connection_mode: str = "socket_mode",
        app_token: Optional[str] = None,
    ) -> ExternalPlatformSchema:
        """Create a Slack platform with proper configuration"""
        if connection_mode not in ("socket_mode", "events_api"):
            raise HTTPException(status_code=400, detail="connection_mode must be socket_mode or events_api")
        if connection_mode == "socket_mode" and not app_token:
            raise HTTPException(status_code=400, detail="Socket Mode requires an app-level token (xapp-…)")
        if connection_mode == "events_api" and not signing_secret:
            raise HTTPException(status_code=400, detail="Events API mode requires the signing secret")

        # Test the bot token first
        test_result = await self._test_slack_token(bot_token)
        if not test_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid bot token: {test_result.get('error')}"
            )

        # Socket mode: prove the app token can actually open a socket URL
        # before saving (same validate-the-whole-chain policy as Google Chat).
        if connection_mode == "socket_mode":
            from app.services.slack_socket_service import open_socket_url
            if not await open_socket_url(app_token):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid app token: apps.connections.open failed — check the xapp- token and its connections:write scope, and that Socket Mode is enabled on the app",
                )

        # Extract team info from test result
        team_info = test_result.get("workspace", {})
        team_id = team_info.get("id")
        team_name = team_info.get("name")

        # Check if platform already exists for this team
        existing_platform = await self.get_platform_by_type(db, organization.id, "slack")
        if existing_platform:
            raise HTTPException(
                status_code=400,
                detail="Slack integration already exists for this organization"
            )

        # Create platform config
        platform_config = {
            "team_id": team_id,
            "team_name": team_name,
            "connection_mode": connection_mode,
            "auto_link_by_email": auto_link_by_email,
        }

        # Create credentials
        credentials = {
            "bot_token": bot_token,
            "signing_secret": signing_secret,
            "app_token": app_token,
        }
        
        # Create platform
        platform = ExternalPlatform(
            organization_id=organization.id,
            platform_type="slack",
            platform_config=platform_config,
            is_active=True
        )
        
        # Encrypt and store credentials
        platform.encrypt_credentials(credentials)
        
        db.add(platform)
        await db.commit()
        await db.refresh(platform)
        # Telemetry: external platform created (slack)
        try:
            await telemetry.capture(
                "external_platform_created",
                {
                    "platform_id": str(platform.id),
                    "platform_type": "slack",
                    "is_active": True,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass
        
        return ExternalPlatformSchema.from_orm(platform)

    async def create_teams_platform(
        self,
        db: AsyncSession,
        organization: Organization,
        app_id: str,
        client_secret: str,
        tenant_id: str,
        current_user: User,
        auto_link_by_email: bool = True,
    ) -> ExternalPlatformSchema:
        """Create a Teams platform with proper configuration"""
        # Test credentials first
        test_result = await self._test_teams_token(app_id, client_secret, tenant_id)
        if not test_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Teams credentials: {test_result.get('error')}",
            )

        # Check if platform already exists for this org
        existing_platform = await self.get_platform_by_type(db, organization.id, "teams")
        if existing_platform:
            raise HTTPException(
                status_code=400,
                detail="Teams integration already exists for this organization",
            )

        platform_config = {
            "tenant_id": tenant_id,
            "app_id": app_id,
            "auto_link_by_email": auto_link_by_email,
        }

        credentials = {
            "app_id": app_id,
            "client_secret": client_secret,
        }

        platform = ExternalPlatform(
            organization_id=organization.id,
            platform_type="teams",
            platform_config=platform_config,
            is_active=True,
        )

        platform.encrypt_credentials(credentials)

        db.add(platform)
        await db.commit()
        await db.refresh(platform)

        try:
            await telemetry.capture(
                "external_platform_created",
                {
                    "platform_id": str(platform.id),
                    "platform_type": "teams",
                    "is_active": True,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        return ExternalPlatformSchema.from_orm(platform)

    async def _test_teams_token(self, app_id: str, client_secret: str, tenant_id: str) -> dict:
        """Test Teams credentials by acquiring an OAuth2 token"""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": app_id,
                        "client_secret": client_secret,
                        "scope": "https://api.botframework.com/.default",
                    },
                )
                if response.status_code == 200 and response.json().get("access_token"):
                    return {"success": True, "app_id": app_id}
                else:
                    error = response.json().get("error_description", "Authentication failed")
                    return {"success": False, "error": error}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Google Chat (Pub/Sub mode) ───────────────────────────────────

    @staticmethod
    def _parse_google_chat_sa(service_account_json) -> dict:
        """Normalize the service-account key to a dict, validating shape."""
        info = service_account_json
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except Exception:
                raise HTTPException(status_code=400, detail="service_account_json is not valid JSON")
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise HTTPException(
                status_code=400,
                detail="service_account_json must be a service-account key (JSON whose type field is service_account)",
            )
        from app.core.egress import check_google_token_uri
        from app.core.url_safety import UnsafeURLError

        try:
            check_google_token_uri(info)
        except UnsafeURLError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return info

    async def _test_google_chat_credentials(self, sa_info: dict, subscription: str) -> dict:
        """Validate the whole chain without saving: mint a chat.bot token
        (proves the key + Chat API), then pull from the subscription (proves
        the Pub/Sub side: subscription exists and the SA is a Subscriber).
        """
        import asyncio as _asyncio
        import httpx

        def _mint(scope: str) -> str:
            from google.oauth2 import service_account
            import google.auth.transport.requests
            from app.core.egress import check_google_token_uri

            check_google_token_uri(sa_info)
            creds = service_account.Credentials.from_service_account_info(
                sa_info, scopes=[scope]
            )
            creds.refresh(google.auth.transport.requests.Request())
            return creds.token

        try:
            await _asyncio.to_thread(_mint, "https://www.googleapis.com/auth/chat.bot")
        except Exception as e:
            return {"success": False, "error": f"chat token failed: {e}"}

        try:
            pubsub_token = await _asyncio.to_thread(
                _mint, "https://www.googleapis.com/auth/pubsub"
            )
        except Exception as e:
            return {"success": False, "error": f"pubsub token failed: {e}"}

        sub = (subscription or "").strip().strip("/")
        if not sub.startswith("projects/") or "/subscriptions/" not in sub:
            return {
                "success": False,
                "error": "pubsub_subscription must look like projects/<project>/subscriptions/<name>",
            }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"https://pubsub.googleapis.com/v1/{sub}:pull",
                    headers={"Authorization": f"Bearer {pubsub_token}"},
                    json={"maxMessages": 1, "returnImmediately": True},
                )
                if resp.status_code != 200:
                    try:
                        err = resp.json().get("error", {}).get("message", f"HTTP {resp.status_code}")
                    except Exception:
                        err = f"HTTP {resp.status_code}"
                    return {"success": False, "error": f"subscription pull failed: {err}"}
        except Exception as e:
            return {"success": False, "error": f"subscription pull failed: {e}"}

        return {"success": True, "client_email": sa_info.get("client_email"), "subscription": sub}

    async def _test_google_chat_connection(self, platform: ExternalPlatform) -> dict:
        creds = platform.decrypt_credentials()
        sa_info = creds.get("service_account_json")
        if isinstance(sa_info, str):
            try:
                sa_info = json.loads(sa_info)
            except Exception:
                sa_info = None
        if not sa_info:
            return {"success": False, "error": "Missing service_account_json"}
        subscription = (platform.platform_config or {}).get("pubsub_subscription", "")
        return await self._test_google_chat_credentials(sa_info, subscription)

    async def create_google_chat_platform(
        self,
        db: AsyncSession,
        organization: Organization,
        data,
        current_user: User,
    ) -> ExternalPlatformSchema:
        """Create a Google Chat integration (Pub/Sub connection mode)."""
        existing_platform = await self.get_platform_by_type(db, organization.id, "google_chat")
        if existing_platform:
            raise HTTPException(
                status_code=400,
                detail="Google Chat integration already exists for this organization",
            )

        sa_info = self._parse_google_chat_sa(data.service_account_json)
        test = await self._test_google_chat_credentials(sa_info, data.pubsub_subscription)
        if not test.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"Google Chat connection failed: {test.get('error')}",
            )

        platform_config = {
            "project_id": sa_info.get("project_id"),
            "client_email": sa_info.get("client_email"),
            "pubsub_subscription": test["subscription"],
            "auto_link_by_email": data.auto_link_by_email,
        }
        credentials = {"service_account_json": sa_info}

        platform = ExternalPlatform(
            organization_id=organization.id,
            platform_type="google_chat",
            platform_config=platform_config,
            is_active=True,
        )
        platform.encrypt_credentials(credentials)

        db.add(platform)
        await db.commit()
        await db.refresh(platform)

        try:
            await telemetry.capture(
                "external_platform_created",
                {
                    "platform_id": str(platform.id),
                    "platform_type": "google_chat",
                    "is_active": True,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        return ExternalPlatformSchema.from_orm(platform)

    async def create_whatsapp_platform(
        self,
        db: AsyncSession,
        organization: Organization,
        access_token: str,
        phone_number_id: str,
        waba_id: str,
        app_secret: str,
        verify_token: str,
        current_user: User,
        name: Optional[str] = None,
    ) -> ExternalPlatformSchema:
        """Create a WhatsApp Cloud API platform with proper configuration.

        An organization may connect several numbers (one per agent/purpose).
        What must stay unique is the ``phone_number_id``: it is the webhook's
        routing key, so two instances sharing it would make inbound routing
        ambiguous. A different number is always allowed.
        """
        # Test the credentials by fetching the phone number metadata
        test_result = await self._test_whatsapp_token(access_token, phone_number_id)
        if not test_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid WhatsApp credentials: {test_result.get('error')}",
            )

        for existing in await self.get_platforms_by_type(db, organization.id, "whatsapp"):
            if (existing.platform_config or {}).get("phone_number_id") == phone_number_id:
                raise HTTPException(
                    status_code=400,
                    detail="This WhatsApp number is already connected to this organization",
                )

        info = test_result.get("info", {})
        platform_config = {
            "phone_number_id": phone_number_id,
            "waba_id": waba_id,
            "display_phone_number": info.get("display_phone_number"),
            "verified_name": info.get("verified_name"),
            "name": name,
        }

        credentials = {
            "access_token": access_token,
            "phone_number_id": phone_number_id,
            "waba_id": waba_id,
            "app_secret": app_secret,
            "verify_token": verify_token,
        }

        platform = ExternalPlatform(
            organization_id=organization.id,
            platform_type="whatsapp",
            platform_config=platform_config,
            is_active=True,
        )
        platform.encrypt_credentials(credentials)

        db.add(platform)
        await db.commit()
        await db.refresh(platform)

        try:
            await telemetry.capture(
                "external_platform_created",
                {
                    "platform_id": str(platform.id),
                    "platform_type": "whatsapp",
                    "is_active": True,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        return ExternalPlatformSchema.from_orm(platform)

    async def _test_whatsapp_connection(self, platform: ExternalPlatform) -> dict:
        """Test WhatsApp connection using stored credentials."""
        try:
            creds = platform.decrypt_credentials()
            return await self._test_whatsapp_token(
                creds.get("access_token"), creds.get("phone_number_id")
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _test_whatsapp_token(self, access_token: str, phone_number_id: str) -> dict:
        """Validate a WhatsApp Cloud API token by calling GET /{phone_number_id}."""
        try:
            import os
            import httpx

            if not access_token or not phone_number_id:
                return {"success": False, "error": "Missing access_token or phone_number_id"}

            base_url = os.environ.get(
                "WHATSAPP_GRAPH_BASE_URL", "https://graph.facebook.com/v20.0"
            )
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{base_url}/{phone_number_id}",
                    params={"fields": "display_phone_number,verified_name"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "success": True,
                        "info": {
                            "display_phone_number": data.get("display_phone_number"),
                            "verified_name": data.get("verified_name"),
                            "id": data.get("id"),
                        },
                    }
                else:
                    try:
                        err = resp.json().get("error", {}).get("message", f"HTTP {resp.status_code}")
                    except Exception:
                        err = f"HTTP {resp.status_code}"
                    return {"success": False, "error": err}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _test_slack_token(self, bot_token: str) -> dict:
        """Test Slack bot token and get workspace info"""
        
        try:
            import httpx
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        return {
                            "success": True,
                            "workspace": {
                                "id": data.get("team_id"),
                                "name": data.get("team")
                            },
                            "bot_user": data.get("user")
                        }
                    else:
                        return {"success": False, "error": data.get("error", "Unknown error")}
                else:
                    return {"success": False, "error": f"HTTP {response.status_code}"}
                    
        except Exception as e:
            return {"success": False, "error": str(e)}