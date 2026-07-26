import asyncio
import json
import time
import httpx
from typing import Any, Dict, List, Optional

from .base_adapter import PlatformAdapter
from app.settings.config import settings

CHAT_API_BASE = "https://chat.googleapis.com/v1"
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"

# Google Chat rejects messages over 4,000 characters; longer answers are
# split into consecutive messages in the same thread.
MAX_TEXT_LEN = 4000


class GoogleChatAdapter(PlatformAdapter):
    """Google Chat platform adapter (Pub/Sub connection mode).

    Inbound events arrive via the Pub/Sub pull listener
    (`google_chat_listener_service`) — there is no webhook route, so
    `verify_webhook_signature` is a no-op. Outbound messages go through the
    Chat REST API authenticated as the integration's service account
    (app auth, `chat.bot` scope).

    Threading model: every Chat message carries `thread.name`; replies are
    posted with `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` so
    they land in the same thread. DMs (`spaceType=DIRECT_MESSAGE`) are mapped
    to channel_type "personal" and reuse a recent report like Teams 1:1
    chats; space messages map to "channel" and use Slack-style
    thread-per-report continuation.
    """

    # {client_email: {"token": str, "expires_at": float}} — shared across
    # instances; adapters are constructed per request.
    _token_cache: Dict[str, Dict[str, Any]] = {}

    # Sender identity captured from inbound events, keyed by external user id
    # ("users/<id>"). Chat has no app-auth user-lookup API, but events carry
    # email + display name for in-domain users, so get_user_info serves from
    # here (same pattern as the WhatsApp adapter's profile-name stash).
    _user_info_cache: Dict[str, Dict[str, Any]] = {}

    # Chat apps can't add reactions, so the 👀/✅ processing indicator is a
    # small marker message posted on receipt and deleted when the completion
    # finishes. Keyed by (channel_id, message_ts) to mirror the reaction API.
    _marker_messages: Dict[str, str] = {}

    def __init__(self, platform):
        super().__init__(platform)
        self.service_account_info = self._load_service_account_info()

    def _load_service_account_info(self) -> Optional[dict]:
        info = (self.credentials or {}).get("service_account_json")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except Exception:
                return None
        return info if isinstance(info, dict) else None

    # ── OAuth token (app auth via service account) ───────────────────

    async def _get_access_token(self) -> Optional[str]:
        if not self.service_account_info:
            print("GOOGLE_CHAT: missing service_account_json credential")
            return None
        cache_key = self.service_account_info.get("client_email", "")
        cached = self._token_cache.get(cache_key)
        if cached and cached["expires_at"] > time.time() + 300:
            return cached["token"]

        def _mint() -> Optional[str]:
            from google.oauth2 import service_account
            import google.auth.transport.requests

            creds = service_account.Credentials.from_service_account_info(
                self.service_account_info, scopes=[CHAT_BOT_SCOPE]
            )
            creds.refresh(google.auth.transport.requests.Request())
            return creds.token

        try:
            token = await asyncio.to_thread(_mint)
            self._token_cache[cache_key] = {
                "token": token,
                "expires_at": time.time() + 3300,
            }
            return token
        except Exception as e:
            print(f"GOOGLE_CHAT: error minting access token: {e}")
            return None

    # ── PlatformAdapter interface ────────────────────────────────────

    async def process_incoming_message(self, event_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize a Google Chat MESSAGE event (Pub/Sub payload) to the
        common shape the ExternalPlatformManager consumes."""
        if event_data.get("type") != "MESSAGE":
            return None

        msg = event_data.get("message") or {}
        sender = msg.get("sender") or event_data.get("user") or {}
        space = msg.get("space") or event_data.get("space") or {}

        # Skip the app's own messages (sender.type == "BOT")
        if sender.get("type") == "BOT":
            return None

        external_user_id = sender.get("name")  # "users/<id>"
        if not external_user_id:
            return None

        # argumentText comes with the leading @mention already stripped;
        # plain text for DMs where there is no mention.
        text = (msg.get("argumentText") or msg.get("text") or "").strip()

        space_type = space.get("spaceType") or space.get("type")
        is_dm = space_type in ("DIRECT_MESSAGE", "DM")
        channel_type = "personal" if is_dm else "channel"

        thread_name = (msg.get("thread") or {}).get("name")

        # Cache sender identity for get_user_info (auto-link by email).
        self._user_info_cache[external_user_id] = {
            "id": external_user_id,
            "name": sender.get("displayName"),
            "email": sender.get("email"),
            "real_name": sender.get("displayName"),
        }

        return {
            "platform_type": "google_chat",
            "external_user_id": external_user_id,
            "external_message_id": msg.get("name"),
            "channel_id": space.get("name"),  # "spaces/<id>"
            "channel_type": channel_type,
            "message_text": text,
            "message_type": "message",
            "timestamp": msg.get("createTime") or event_data.get("eventTime"),
            # Thread context: thread.name is stable across replies in the
            # thread, so it doubles as the thread key on both DMs and spaces.
            "thread_ts": thread_name,
            "message_ts": msg.get("name"),
            "is_thread_reply": bool(msg.get("threadReply")),
        }

    @staticmethod
    def _split_text(text: str) -> List[str]:
        """Split text into <=MAX_TEXT_LEN chunks, preferring newline breaks."""
        if len(text) <= MAX_TEXT_LEN:
            return [text]
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= MAX_TEXT_LEN:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, MAX_TEXT_LEN)
            if cut < MAX_TEXT_LEN // 2:
                cut = MAX_TEXT_LEN
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        return chunks

    async def _post_message(
        self, space_name: str, text: str, thread_name: Optional[str] = None
    ) -> Optional[dict]:
        """POST one message; returns the created Message resource or None."""
        token = await self._get_access_token()
        if not token or not space_name:
            return None
        body: Dict[str, Any] = {"text": text}
        params = {}
        if thread_name:
            body["thread"] = {"name": thread_name}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{CHAT_API_BASE}/{space_name}/messages",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
                if resp.status_code == 200:
                    return resp.json()
                print(f"GOOGLE_CHAT: send failed {resp.status_code} - {resp.text[:300]}")
                return None
        except Exception as e:
            print(f"GOOGLE_CHAT: error sending message: {e}")
            return None

    async def send_response(self, message_data: Dict[str, Any]) -> bool:
        space = message_data.get("channel") or message_data.get("channel_id")
        text = message_data.get("content") or message_data.get("text") or ""
        if not space or not text:
            print("GOOGLE_CHAT: send_response missing space or text")
            return False
        thread = message_data.get("thread_ts")
        ok = True
        for chunk in self._split_text(text):
            ok = (await self._post_message(space, chunk, thread)) is not None and ok
        return ok

    async def get_user_info(self, external_user_id: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """Serve sender identity captured from the inbound event.

        Chat's app auth has no user-directory endpoint, but MESSAGE events
        include email/displayName for in-domain Workspace users — enough for
        the manager's auto-link-by-email flow.
        """
        return self._user_info_cache.get(external_user_id, {})

    async def verify_webhook_signature(self, request_body: bytes, signature: str, timestamp: str) -> bool:
        """No inbound HTTP in Pub/Sub mode — the authenticated pull
        subscription is the trust boundary."""
        return True

    async def send_verification_message(self, channel_id: str, email: str, token: str) -> bool:
        base_url = settings.bow_config.base_url
        verification_url = f"{base_url}/settings/integrations/verify/{token}"
        text = (
            "*Account Verification Required*\n\n"
            "To start using this bot, please verify your account: "
            f"<{verification_url}|Verify Account>"
        )
        return (await self._post_message(channel_id, text)) is not None

    # ── Reaction stand-ins (marker message pattern) ──────────────────

    @staticmethod
    def _thread_name_for_message(message_name: Optional[str]) -> Optional[str]:
        """Derive a message's thread resource name from its own name.

        ``spaces/X/messages/ABC.DEF`` lives in thread ``spaces/X/threads/ABC``
        (the segment before the dot is the thread id — observed in live
        payloads and stable across root messages and replies).
        """
        if not message_name or "/messages/" not in message_name:
            return None
        space, msg_id = message_name.split("/messages/", 1)
        return f"{space}/threads/{msg_id.split('.', 1)[0]}"

    async def add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        """Chat apps can't react to messages. "eyes" posts a small working
        marker (threaded under the user's message) instead;
        "white_check_mark" deletes it (completion finished)."""
        key = f"{channel_id}:{timestamp}"
        if emoji == "eyes":
            thread = self._thread_name_for_message(timestamp)
            created = await self._post_message(channel_id, "👀 _Working on it…_", thread)
            if created and created.get("name"):
                self._marker_messages[key] = created["name"]
            return created is not None
        if emoji == "white_check_mark":
            marker = self._marker_messages.pop(key, None)
            if marker:
                deleted = await self._delete_message(marker)
                print(f"GOOGLE_CHAT: working-marker cleanup for {key}: {deleted}")
            return True
        return True

    async def remove_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        return True

    async def _delete_message(self, message_name: str) -> bool:
        token = await self._get_access_token()
        if not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.delete(
                    f"{CHAT_API_BASE}/{message_name}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                return resp.status_code == 200
        except Exception as e:
            print(f"GOOGLE_CHAT: error deleting message: {e}")
            return False

    # ── Convenience methods (manager / notification pipeline) ────────

    async def send_dm(self, user_id: str, text: str) -> bool:
        """Send to the user's DM space with the app.

        Replies always target the space the message arrived from
        (channel_id), so this path is only used for out-of-band notices; it
        finds the existing DM via spaces.findDirectMessage.
        """
        space = await self._find_direct_message_space(user_id)
        if not space:
            print(f"GOOGLE_CHAT: no DM space found for {user_id}")
            return False
        return await self.send_response({"channel": space, "text": text})

    async def has_dm_space(self, user_id: str) -> bool:
        """Whether the app can DM this user right now. Chat apps can't open a
        DM the user (or an admin install) hasn't created — proactive senders
        use this to fall through to another channel instead of failing."""
        return (await self._find_direct_message_space(user_id)) is not None

    async def _find_direct_message_space(self, user_id: str) -> Optional[str]:
        token = await self._get_access_token()
        if not token:
            return None
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{CHAT_API_BASE}/spaces:findDirectMessage",
                    params={"name": user_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    return resp.json().get("name")
                return None
        except Exception as e:
            print(f"GOOGLE_CHAT: error finding DM space: {e}")
            return None

    async def send_dm_in_thread(
        self, user_id: str, text: str, thread_ts: str = None, channel_id: str = None
    ) -> bool:
        if not channel_id:
            channel_id = await self._find_direct_message_space(user_id)
            if not channel_id:
                return False
        return await self.send_response(
            {"channel": channel_id, "text": text, "thread_ts": thread_ts}
        )

    async def send_file_in_thread(
        self, user_id: str, file_path: str, title: str, thread_ts: str = None, channel_id: str = None
    ) -> bool:
        """Chat's media upload rejects app-auth credentials, so files can't be
        attached directly (verified 2026-07; same limitation Hermes documents).
        Point the user at the web report instead."""
        text = f"*{title}*\n_The full file is available in the web report._"
        return await self.send_dm_in_thread(user_id, text, thread_ts, channel_id=channel_id)

    async def send_file_in_dm(self, user_id: str, file_path: str, title: str) -> bool:
        return await self.send_file_in_thread(user_id, file_path, title)

    async def send_image_in_dm(self, user_id: str, image_path: str, title: str) -> bool:
        return await self.send_file_in_thread(user_id, image_path, title)
