import re
import time
import os
import httpx
import jwt
from typing import Dict, Any, Optional
from .base_adapter import PlatformAdapter
from app.settings.config import settings


class TeamsAdapter(PlatformAdapter):
    """Microsoft Teams platform adapter using Bot Connector REST API v4"""

    # Class-level caches shared across instances
    _token_cache: Dict[str, Any] = {"token": "", "expires_at": 0}
    _jwks_cache: Dict[str, Any] = {"keys": None, "fetched_at": 0}

    OPENID_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
    TOKEN_SCOPE = "https://api.botframework.com/.default"
    EXPECTED_ISSUER = "https://api.botframework.com"
    JWKS_CACHE_TTL = 86400  # 24 hours

    def __init__(self, platform):
        super().__init__(platform)
        self.app_id = self.credentials.get("app_id") or self.config.get("app_id")
        self.client_secret = self.credentials.get("client_secret")
        self.tenant_id = self.config.get("tenant_id")
        self.service_url = self.config.get("service_url", "https://smba.trafficmanager.net/teams/")
        # Bot identity as seen in Teams conversations (e.g. "28:{app_id}")
        # Persisted on platform_config after first inbound message
        self.bot_id = self.config.get("bot_id", self.app_id)
        self.bot_name = self.config.get("bot_name", "")

    # ── OAuth2 Token Management (outbound auth) ──────────────────────

    async def _get_access_token(self) -> Optional[str]:
        """Get a cached or fresh OAuth2 access token for Bot Connector API calls."""
        if (
            self._token_cache["token"]
            and self._token_cache["expires_at"] > time.time() + 300
        ):
            return self._token_cache["token"]

        try:
            token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.app_id,
                        "client_secret": self.client_secret,
                        "scope": self.TOKEN_SCOPE,
                    },
                )
                if response.status_code == 200:
                    data = response.json()
                    TeamsAdapter._token_cache = {
                        "token": data["access_token"],
                        "expires_at": time.time() + data.get("expires_in", 3600),
                    }
                    return data["access_token"]
                else:
                    print(f"TEAMS: Failed to get access token: {response.status_code} {response.text}")
                    return None
        except Exception as e:
            print(f"TEAMS: Error getting access token: {e}")
            return None

    # ── JWT Verification (inbound auth) ──────────────────────────────

    async def _get_signing_keys(self, force_refresh: bool = False):
        """Fetch Microsoft's JWT signing keys from OpenID metadata."""
        if (
            not force_refresh
            and self._jwks_cache["keys"]
            and (time.time() - self._jwks_cache["fetched_at"]) < self.JWKS_CACHE_TTL
        ):
            return self._jwks_cache["keys"]

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Get OpenID metadata to find jwks_uri
                meta_resp = await client.get(self.OPENID_METADATA_URL)
                metadata = meta_resp.json()
                jwks_uri = metadata["jwks_uri"]

                # Fetch the actual signing keys
                keys_resp = await client.get(jwks_uri)
                jwks = keys_resp.json()

                TeamsAdapter._jwks_cache = {
                    "keys": jwks,
                    "fetched_at": time.time(),
                }
                return jwks
        except Exception as e:
            print(f"TEAMS: Error fetching signing keys: {e}")
            return self._jwks_cache.get("keys")

    async def verify_webhook_signature(self, request_body: bytes, signature: str, timestamp: str) -> bool:
        """Verify inbound JWT token from Bot Framework.

        Args:
            request_body: Raw request body (used to extract serviceUrl for claim validation)
            signature: The full Authorization header value ("Bearer <jwt>")
            timestamp: Unused for Teams (kept for interface compatibility)
        """
        if not signature or not signature.startswith("Bearer "):
            print("TEAMS: Missing or invalid Authorization header")
            return False

        token = signature[7:]  # Strip "Bearer "

        # Try with cached keys first, then refresh if validation fails
        for attempt in range(2):
            try:
                jwks = await self._get_signing_keys(force_refresh=(attempt == 1))
                if not jwks:
                    return False

                # Build signing keys from JWKS
                public_keys = {}
                for key_data in jwks.get("keys", []):
                    kid = key_data.get("kid")
                    if kid:
                        public_keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

                # Decode header to find the key ID
                unverified_header = jwt.get_unverified_header(token)
                kid = unverified_header.get("kid")
                if kid not in public_keys:
                    if attempt == 0:
                        continue  # Try refreshing keys
                    print(f"TEAMS: Signing key {kid} not found in JWKS")
                    return False

                # Validate the JWT
                # Accept tokens from Bot Framework and Azure AD (Web Chat/Emulator)
                allowed_issuers = [
                    self.EXPECTED_ISSUER,
                    f"https://sts.windows.net/{self.tenant_id}/",
                    "https://login.microsoftonline.com/{}/v2.0".format(self.tenant_id),
                ]
                decoded = jwt.decode(
                    token,
                    key=public_keys[kid],
                    algorithms=["RS256"],
                    audience=self.app_id,
                    options={"verify_exp": True, "verify_iss": False},
                    leeway=300,  # 5-minute clock skew
                )
                # Manual issuer check against allowed list
                token_issuer = decoded.get("iss", "")
                if token_issuer not in allowed_issuers:
                    print(f"TEAMS: Unexpected JWT issuer: {token_issuer}")
                    return False

                # Validate serviceUrl claim matches activity's serviceUrl
                import json
                try:
                    activity = json.loads(request_body)
                    activity_service_url = activity.get("serviceUrl", "")
                    token_service_url = decoded.get("serviceurl", "")
                    if token_service_url and activity_service_url:
                        if token_service_url.rstrip("/") != activity_service_url.rstrip("/"):
                            print(f"TEAMS: serviceUrl mismatch: token={token_service_url}, activity={activity_service_url}")
                            return False
                except (json.JSONDecodeError, AttributeError):
                    pass

                return True

            except jwt.ExpiredSignatureError:
                print("TEAMS: JWT token has expired")
                return False
            except jwt.InvalidTokenError as e:
                if attempt == 0:
                    continue  # Try refreshing keys
                print(f"TEAMS: JWT validation failed: {e}")
                return False

        return False

    # ── Abstract Method Implementations ──────────────────────────────

    async def process_incoming_message(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process incoming Teams Activity into normalized message dict."""
        activity_type = event_data.get("type", "")

        # Extract user and conversation info
        from_user = event_data.get("from", {})
        conversation = event_data.get("conversation", {})
        channel_data = event_data.get("channelData", {})

        # Skip bot's own messages
        if from_user.get("id") == self.app_id:
            print("TEAMS: Ignoring bot's own message")
            return None

        # Determine channel type from conversation
        conversation_type = conversation.get("conversationType", "personal")
        channel_type = "personal" if conversation_type == "personal" else "channel"

        # Strip bot mention tags from message text
        text = event_data.get("text", "") or ""
        text = re.sub(r"<at>[^<]*</at>\s*", "", text).strip()

        # Extract serviceUrl (critical for outbound calls)
        service_url = event_data.get("serviceUrl", self.service_url)

        # Capture the bot's identity from the "recipient" field
        # In real Teams this is "28:{app_id}", in Web Chat it's just the app_id
        bot_recipient = event_data.get("recipient", {})

        # Determine thread_ts based on conversation type:
        # - Personal chats: use conversation ID (stable, no threading)
        # - Channel messages: use replyToId for thread replies, or activity ID for root messages
        #   so thread replies can be matched back to the root message's completion
        reply_to_id = event_data.get("replyToId")
        conv_id = conversation.get("id", "")

        # In Teams channels, conversation IDs include the root message:
        #   19:abc@thread.tacv2;messageid=<root_msg_id>
        # Extract messageid to use as a stable thread identifier
        thread_msg_id = None
        if ";messageid=" in conv_id:
            thread_msg_id = conv_id.split(";messageid=")[-1]

        is_thread_reply = bool(reply_to_id) or bool(thread_msg_id)

        if channel_type == "personal":
            thread_ts = conv_id
        elif thread_msg_id:
            thread_ts = thread_msg_id
        elif reply_to_id:
            thread_ts = reply_to_id
        else:
            thread_ts = event_data.get("id")

        return {
            "platform_type": "teams",
            "external_user_id": from_user.get("id"),
            "external_message_id": event_data.get("id"),
            "channel_id": conversation.get("id"),
            "channel_type": channel_type,
            "message_text": text,
            "message_type": activity_type,
            "timestamp": event_data.get("timestamp"),
            "team_id": channel_data.get("tenant", {}).get("id", self.tenant_id),
            # Thread context
            "service_url": service_url,
            "thread_ts": thread_ts,
            "message_ts": event_data.get("id"),   # Activity ID for reactions/replies
            "is_thread_reply": is_thread_reply,
            # Bot identity for outbound replies
            "bot_id": bot_recipient.get("id", self.app_id),
            "bot_name": bot_recipient.get("name", ""),
        }

    async def send_response(self, message_data: Dict[str, Any]) -> bool:
        """Send a message via Bot Connector REST API."""
        try:
            token = await self._get_access_token()
            if not token:
                print("TEAMS: No access token available")
                return False

            service_url = message_data.get("service_url", self.service_url)
            conversation_id = message_data.get("channel") or message_data.get("channel_id")
            if not conversation_id:
                print("TEAMS: No conversation_id specified")
                return False

            # Build activity payload
            # Use bot_id from instance (persisted from first inbound activity)
            activity = {
                "type": "message",
                "from": {"id": self.bot_id, "name": self.bot_name} if self.bot_name else {"id": self.bot_id},
                "conversation": {"id": conversation_id},
                "text": message_data.get("content", "") or message_data.get("text", ""),
                "textFormat": "markdown",
            }

            # Add threading via replyToId
            if message_data.get("thread_ts"):
                activity["replyToId"] = message_data["thread_ts"]

            # Add attachments (Adaptive Cards, files, etc.)
            if message_data.get("attachments"):
                activity["attachments"] = message_data["attachments"]

            url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
            print(f"TEAMS: Sending message to {url}")

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=activity,
                )

                print(f"TEAMS: API response status: {response.status_code}")
                if response.status_code in (200, 201):
                    return True
                else:
                    print(f"TEAMS: API error: {response.status_code} - {response.text}")
                    return False

        except httpx.ConnectTimeout:
            print("TEAMS: API connection timeout")
            return False
        except httpx.ReadTimeout:
            print("TEAMS: API read timeout")
            return False
        except Exception as e:
            print(f"TEAMS: Error sending message: {e}")
            return False

    async def get_user_info(self, external_user_id: str) -> Dict[str, Any]:
        """Get Teams user information via Bot Connector API."""
        try:
            token = await self._get_access_token()
            if not token:
                return {}

            # We need a conversation_id to query members; use stored config
            # This is typically called in context where we have a conversation
            # For now, return basic info from what we have
            url = f"{self.service_url.rstrip('/')}/v3/conversations/placeholder/members/{external_user_id}"

            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 200:
                    user_data = response.json()
                    return {
                        "id": user_data.get("id"),
                        "name": user_data.get("name"),
                        "email": user_data.get("email"),
                        "real_name": user_data.get("givenName", "") + " " + user_data.get("surname", ""),
                    }
            return {}
        except Exception as e:
            print(f"TEAMS: Error getting user info: {e}")
            return {}

    async def send_verification_message(self, channel_id: str, email: str, token: str) -> bool:
        """Send verification Adaptive Card to Teams user."""
        try:
            base_url = settings.bow_config.base_url
            verification_url = f"{base_url}/settings/integrations/verify/{token}"

            message = {
                "channel": channel_id,
                "service_url": self.service_url,
                "text": f"To start using this bot, please verify your account: {verification_url}",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [
                                {
                                    "type": "TextBlock",
                                    "text": "Account Verification Required",
                                    "weight": "Bolder",
                                    "size": "Medium",
                                },
                                {
                                    "type": "TextBlock",
                                    "text": "To start using this bot, please click the button below to verify your account.",
                                    "wrap": True,
                                },
                            ],
                            "actions": [
                                {
                                    "type": "Action.OpenUrl",
                                    "title": "Verify Account",
                                    "url": verification_url,
                                }
                            ],
                        },
                    }
                ],
            }

            print(f"TEAMS: Sending verification message to conversation {channel_id}")
            print(f"TEAMS: Verification URL: {verification_url}")
            return await self.send_response(message)

        except Exception as e:
            print(f"TEAMS: Error sending verification message: {e}")
            return False

    # ── Non-abstract Methods (matching Slack adapter interface) ──────

    async def add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        """No-op: Teams bots cannot add emoji reactions."""
        return True

    async def remove_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        """No-op: Teams bots cannot remove emoji reactions."""
        return True

    async def _create_conversation(self, user_id: str) -> Optional[str]:
        """Create a 1:1 conversation with a user and return the conversation ID."""
        token = await self._get_access_token()
        if not token:
            return None

        try:
            url = f"{self.service_url.rstrip('/')}/v3/conversations"
            payload = {
                "bot": {"id": self.app_id},
                "members": [{"id": user_id}],
                "channelData": {"tenant": {"id": self.tenant_id}},
                "isGroup": False,
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if response.status_code in (200, 201):
                    data = response.json()
                    return data.get("id")
                else:
                    print(f"TEAMS: Failed to create conversation: {response.status_code} {response.text}")
                    return None
        except Exception as e:
            print(f"TEAMS: Error creating conversation: {e}")
            return None

    async def send_dm(self, user_id: str, text: str) -> bool:
        """Send a direct message to a user."""
        return await self.send_dm_in_thread(user_id, text)

    async def send_dm_in_thread(
        self, user_id: str, text: str, thread_ts: str = None, channel_id: str = None
    ) -> bool:
        """Send a message, optionally as a reply.

        If channel_id is provided, sends to that conversation.
        Otherwise, creates a 1:1 conversation with the user.
        """
        try:
            if not channel_id:
                channel_id = await self._create_conversation(user_id)
                if not channel_id:
                    return False

            message_data = {
                "channel": channel_id,
                "text": text,
                "service_url": self.service_url,
            }

            if thread_ts:
                message_data["thread_ts"] = thread_ts

            return await self.send_response(message_data)
        except Exception as e:
            print(f"TEAMS: Error sending DM in thread: {e}")
            return False

    async def send_file_in_dm(self, user_id: str, file_path: str, title: str) -> bool:
        """Send a file in a direct message."""
        return await self.send_file_in_thread(user_id, file_path, title)

    async def send_file_in_thread(
        self, user_id: str, file_path: str, title: str, thread_ts: str = None, channel_id: str = None
    ) -> bool:
        """Send a file, optionally as a reply.

        Uses the Bot Connector attachment upload API for files.
        """
        if not os.path.exists(file_path):
            print(f"TEAMS: File not found at {file_path}")
            return False

        try:
            token = await self._get_access_token()
            if not token:
                return False

            if not channel_id:
                channel_id = await self._create_conversation(user_id)
                if not channel_id:
                    return False

            import base64
            import mimetypes

            file_name = os.path.basename(file_path)
            mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            file_size = os.path.getsize(file_path)

            # For small files (<4MB), send as inline base64 attachment
            if file_size < 4 * 1024 * 1024:
                with open(file_path, "rb") as f:
                    file_data = base64.b64encode(f.read()).decode("utf-8")

                activity = {
                    "type": "message",
                    "text": title,
                    "textFormat": "markdown",
                    "attachments": [
                        {
                            "contentType": mime_type,
                            "contentUrl": f"data:{mime_type};base64,{file_data}",
                            "name": file_name,
                        }
                    ],
                }

                if thread_ts:
                    activity["replyToId"] = thread_ts

                url = f"{self.service_url.rstrip('/')}/v3/conversations/{channel_id}/activities"

                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                        json=activity,
                    )
                    if response.status_code in (200, 201):
                        return True
                    print(f"TEAMS: Failed to send file: {response.status_code} {response.text}")
                    return False
            else:
                # For larger files, send a message with a note
                await self.send_dm_in_thread(
                    user_id,
                    f"File *{file_name}* is too large to send directly. Please check the web dashboard.",
                    thread_ts,
                    channel_id=channel_id,
                )
                return True

        except Exception as e:
            print(f"TEAMS: Error sending file: {e}")
            return False

    async def send_image_in_dm(self, user_id: str, image_path: str, title: str) -> bool:
        """Send an image in a direct message."""
        return await self.send_file_in_dm(user_id, image_path, title)
