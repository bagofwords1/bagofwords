from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from fastapi import HTTPException
from typing import List, Optional
import json

from app.models.external_platform import ExternalPlatform
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
        """Get platform by type for an organization"""
        
        stmt = select(ExternalPlatform).where(
            and_(
                ExternalPlatform.organization_id == organization_id,
                ExternalPlatform.platform_type == platform_type
            )
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
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
        """Default SMTP/IMAP hosts for the OAuth providers."""
        if auth_type == "microsoft":
            return {
                "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls",
                "imap_host": "outlook.office365.com", "imap_port": 993, "imap_use_ssl": True,
            }
        if auth_type == "google":
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
        import aiosmtplib
        from app.services.email.sender import SmtpConfig
        from app.services.email.mailbox_reader import ImapConfig
        from app.services.email.oauth import OAuthSettings, get_xoauth2_string

        result = {"success": True, "smtp": None, "imap": None}
        auth_type = creds.get("auth_type") or cfg.get("auth_type") or "password"
        oauth = OAuthSettings.from_credentials(creds, cfg)

        # Mint the token once (reused for SMTP + IMAP) for OAuth auth types.
        sasl = None
        if auth_type in ("microsoft", "google"):
            try:
                sasl = await get_xoauth2_string(oauth)
            except Exception as e:
                return {"success": False, "smtp": f"oauth token failed: {e}", "imap": None}

        # SMTP probe.
        try:
            smtp = SmtpConfig.from_credentials(creds, cfg).resolved()
            client = aiosmtplib.SMTP(
                hostname=smtp.host, port=smtp.port,
                use_tls=(smtp.security == "ssl"), timeout=15,
            )
            await client.connect()
            if smtp.security == "starttls":
                try:
                    await client.starttls()
                except Exception:
                    pass
            if sasl is not None:
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
                    import imaplib
                    conn = (
                        imaplib.IMAP4_SSL(imap.host, imap.port)
                        if imap.use_ssl else imaplib.IMAP4(imap.host, imap.port)
                    )
                    if sasl is not None:
                        conn.authenticate("XOAUTH2", lambda _c: sasl.encode("ascii"))
                    else:
                        conn.login(imap.username, imap.password)
                    conn.select(imap.mailbox)
                    conn.logout()

                await asyncio.to_thread(_probe)
                result["imap"] = "ok"
            except Exception as e:
                return {"success": False, "smtp": "ok", "imap": f"failed: {e}"}

        return result

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

        auth_type = data.auth_type or "password"
        defaults = self._email_provider_defaults(auth_type)
        from_address = data.from_address or data.smtp_username or data.imap_username

        # Resolve effective hosts/ports (provider defaults for OAuth types).
        smtp_host = data.smtp_host or defaults.get("smtp_host")
        smtp_port = data.smtp_port or defaults.get("smtp_port") or 587
        smtp_security = data.smtp_security or defaults.get("smtp_security") or "starttls"
        imap_host = data.imap_host or defaults.get("imap_host")
        imap_port = data.imap_port or defaults.get("imap_port") or 993
        imap_use_ssl = data.imap_use_ssl if data.imap_use_ssl is not None else True

        # For OAuth the mailbox address is the SMTP/IMAP "user"; default it.
        smtp_username = data.smtp_username or (from_address if auth_type != "password" else None)
        imap_username = data.imap_username or (from_address if auth_type != "password" else None)

        # Inbound: for OAuth, hosts are defaulted so the explicit toggle decides;
        # for password it's inferred from IMAP creds (or the toggle).
        if auth_type == "password":
            inbound_enabled = bool(imap_host and imap_username) or bool(data.inbound_enabled)
        else:
            inbound_enabled = bool(data.inbound_enabled)

        # Parse the Google service-account JSON (accept a dict or a JSON string).
        google_sa = data.google_service_account_json
        if isinstance(google_sa, str) and google_sa.strip():
            try:
                google_sa = json.loads(google_sa)
            except Exception:
                raise HTTPException(status_code=400, detail="google_service_account_json is not valid JSON")

        # Non-secret config (drives capability + channel behavior).
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
            # Non-secret OAuth identifiers (kept in config for display/refresh).
            "ms_tenant_id": data.ms_tenant_id,
            "ms_client_id": data.ms_client_id,
            "capabilities": ["send", "receive"] if inbound_enabled else ["send"],
        }

        # Secrets (encrypted at rest).
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
            # OAuth secrets
            "ms_tenant_id": data.ms_tenant_id,
            "ms_client_id": data.ms_client_id,
            "ms_client_secret": data.ms_client_secret,
            "google_service_account_info": google_sa,
        }

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
        signing_secret: str,
        current_user: User,
        auto_link_by_email: bool = True,
    ) -> ExternalPlatformSchema:
        """Create a Slack platform with proper configuration"""
        # Test the bot token first
        test_result = await self._test_slack_token(bot_token)
        if not test_result.get("success"):
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid bot token: {test_result.get('error')}"
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
            "base_url": "https://your-domain.com",  # Update this
            "auto_link_by_email": auto_link_by_email,
        }
        
        # Create credentials
        credentials = {
            "bot_token": bot_token,
            "signing_secret": signing_secret
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
    ) -> ExternalPlatformSchema:
        """Create a WhatsApp Cloud API platform with proper configuration."""
        # Test the credentials by fetching the phone number metadata
        test_result = await self._test_whatsapp_token(access_token, phone_number_id)
        if not test_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid WhatsApp credentials: {test_result.get('error')}",
            )

        existing_platform = await self.get_platform_by_type(db, organization.id, "whatsapp")
        if existing_platform:
            raise HTTPException(
                status_code=400,
                detail="WhatsApp integration already exists for this organization",
            )

        info = test_result.get("info", {})
        platform_config = {
            "phone_number_id": phone_number_id,
            "waba_id": waba_id,
            "display_phone_number": info.get("display_phone_number"),
            "verified_name": info.get("verified_name"),
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