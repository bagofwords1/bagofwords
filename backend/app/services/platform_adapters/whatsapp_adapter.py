import hmac
import hashlib
import os
import mimetypes
import httpx
from typing import Dict, Any, Optional
from .base_adapter import PlatformAdapter
from app.settings.config import settings


def _graph_base_url() -> str:
    """Meta Graph API base URL. Overridable via env for sandbox / tests."""
    return os.environ.get("WHATSAPP_GRAPH_BASE_URL", "https://graph.facebook.com/v20.0")


class WhatsAppAdapter(PlatformAdapter):
    """WhatsApp Cloud API platform adapter.

    Mirrors SlackAdapter's surface area so the ExternalPlatformManager and
    notification pipeline can drive it unchanged.

    Threading model: WhatsApp has no native thread_ts. We use the *original*
    inbound message id as the conversational root and stamp it onto
    Completion.external_thread_ts. All outbound replies set
    `context.message_id = <root>` so the WhatsApp UI shows quoted replies
    chained to the root message.
    """

    def __init__(self, platform):
        super().__init__(platform)

    # ---------- helpers ----------

    def _phone_number_id(self) -> Optional[str]:
        return (self.credentials or {}).get("phone_number_id") or (
            self.config or {}
        ).get("phone_number_id")

    def _access_token(self) -> Optional[str]:
        return (self.credentials or {}).get("access_token")

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }

    async def _post_message(self, payload: Dict[str, Any]) -> bool:
        phone_number_id = self._phone_number_id()
        if not phone_number_id or not self._access_token():
            print("WhatsApp: missing phone_number_id or access_token")
            return False
        url = f"{_graph_base_url()}/{phone_number_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=self._auth_headers(), json=payload)
                if resp.status_code == 200:
                    return True
                print(f"WhatsApp API error: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            print(f"Error posting WhatsApp message: {e}")
            return False

    # ---------- PlatformAdapter interface ----------

    async def process_incoming_message(self, event_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse a WhatsApp Cloud API webhook payload.

        Expected shape:
          {"object":"whatsapp_business_account",
           "entry":[{"id":"<waba_id>",
                     "changes":[{"field":"messages",
                                 "value":{"metadata":{"phone_number_id":"...","display_phone_number":"..."},
                                          "contacts":[{"wa_id":"...","profile":{"name":"..."}}],
                                          "messages":[{"from":"...","id":"wamid...",
                                                       "timestamp":"...","type":"text",
                                                       "text":{"body":"hi"},
                                                       "context":{"id":"wamid_parent"}}]}}]}]}

        Returns None for statuses-only / unsupported payloads.
        """
        try:
            entry = (event_data.get("entry") or [{}])[0]
            change = (entry.get("changes") or [{}])[0]
            value = change.get("value") or {}
            messages = value.get("messages") or []
            if not messages:
                # Status-only payload (delivered/read) or empty
                return None
            msg = messages[0]
            msg_type = msg.get("type")
            if msg_type != "text":
                # For v1 we only support inbound text. Non-text messages are
                # acknowledged but not routed to the agent.
                print(f"WhatsApp: unsupported inbound message type: {msg_type}")
                return None

            wa_id = msg.get("from")
            message_id = msg.get("id")
            text = (msg.get("text") or {}).get("body", "")
            context_id = (msg.get("context") or {}).get("id")

            # Threading: if user replied to a previous message, use that id as
            # thread root; otherwise this message starts a new thread.
            is_thread_reply = bool(context_id)
            effective_thread = context_id if is_thread_reply else message_id

            # Populate contact name if present (WhatsApp has no user-info API)
            contacts = value.get("contacts") or []
            profile_name = None
            if contacts:
                profile_name = (contacts[0].get("profile") or {}).get("name")
            if profile_name:
                # stash on adapter for get_user_info()
                self._last_profile_name = profile_name

            metadata = value.get("metadata") or {}

            return {
                "platform_type": "whatsapp",
                "external_user_id": wa_id,
                "external_message_id": message_id,
                # WhatsApp has no channels — the wa_id *is* the DM address.
                "channel_id": wa_id,
                "channel_type": "im",
                "message_text": text,
                "message_type": "message",
                "timestamp": msg.get("timestamp"),
                "phone_number_id": metadata.get("phone_number_id"),
                # Thread context
                "thread_ts": effective_thread,
                "message_ts": message_id,
                "is_thread_reply": is_thread_reply,
                "profile_name": profile_name,
            }
        except Exception as e:
            print(f"Error processing WhatsApp message: {e}")
            return None

    async def send_response(self, message_data: Dict[str, Any]) -> bool:
        """Send a text message via WhatsApp Cloud API."""
        to = message_data.get("channel") or message_data.get("channel_id") or message_data.get("to")
        text = message_data.get("text") or message_data.get("content") or ""
        if not to or not text:
            print("WhatsApp: send_response missing 'to' or 'text'")
            return False

        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": text[:4096], "preview_url": False},
        }
        thread_ts = message_data.get("thread_ts")
        if thread_ts:
            payload["context"] = {"message_id": thread_ts}
        return await self._post_message(payload)

    async def get_user_info(self, external_user_id: str) -> Dict[str, Any]:
        """WhatsApp Cloud API does not expose a user-info endpoint.

        We return whatever profile name we captured from the last inbound
        event (if any). Email is never available.
        """
        name = getattr(self, "_last_profile_name", None)
        return {
            "id": external_user_id,
            "name": name,
            "email": None,
            "real_name": name,
        }

    async def verify_webhook_signature(
        self, request_body: bytes, signature: str, timestamp: str
    ) -> bool:
        """Verify X-Hub-Signature-256 HMAC-SHA256 header from Meta."""
        try:
            app_secret = (self.credentials or {}).get("app_secret")
            if not app_secret or not signature:
                return False
            expected = "sha256=" + hmac.new(
                app_secret.encode("utf-8"),
                request_body,
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, signature)
        except Exception as e:
            print(f"Error verifying WhatsApp signature: {e}")
            return False

    async def send_verification_message(self, channel_id: str, email: str, token: str) -> bool:
        base_url = settings.bow_config.base_url
        verification_url = f"{base_url}/settings/integrations/verify/{token}"
        text = (
            "Account Verification Required\n\n"
            "To start using this bot, please verify your account by opening "
            f"this link:\n{verification_url}"
        )
        return await self.send_response({"to": channel_id, "text": text})

    # ---------- convenience methods used by the manager / notification pipeline ----------

    async def add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        """Add a reaction to a WhatsApp message.

        `emoji` is a Slack emoji name (e.g. "eyes"). We map common names to
        unicode so the SlackAdapter-driven flow works transparently.
        """
        emoji_char = _SLACK_EMOJI_MAP.get(emoji, emoji)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": channel_id,
            "type": "reaction",
            "reaction": {"message_id": timestamp, "emoji": emoji_char},
        }
        return await self._post_message(payload)

    async def remove_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        """Remove a reaction (Cloud API convention: empty emoji string)."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": channel_id,
            "type": "reaction",
            "reaction": {"message_id": timestamp, "emoji": ""},
        }
        return await self._post_message(payload)

    async def send_dm(self, user_id: str, text: str) -> bool:
        return await self.send_response({"to": user_id, "text": text})

    async def send_dm_in_thread(
        self,
        user_id: str,
        text: str,
        thread_ts: str = None,
        channel_id: str = None,
    ) -> bool:
        to = channel_id or user_id
        return await self.send_response({"to": to, "text": text, "thread_ts": thread_ts})

    async def _upload_media(self, file_path: str) -> Optional[str]:
        """Upload media and return its media id."""
        phone_number_id = self._phone_number_id()
        access_token = self._access_token()
        if not phone_number_id or not access_token:
            return None
        if not os.path.exists(file_path):
            print(f"WhatsApp: file not found at {file_path}")
            return None
        mime, _ = mimetypes.guess_type(file_path)
        mime = mime or "application/octet-stream"
        url = f"{_graph_base_url()}/{phone_number_id}/media"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, "rb") as f:
                    files = {
                        "file": (os.path.basename(file_path), f, mime),
                        "messaging_product": (None, "whatsapp"),
                        "type": (None, mime),
                    }
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {access_token}"},
                        files=files,
                    )
                if resp.status_code == 200:
                    return resp.json().get("id")
                print(f"WhatsApp media upload failed: {resp.status_code} - {resp.text}")
                return None
        except Exception as e:
            print(f"Error uploading WhatsApp media: {e}")
            return None

    async def send_file_in_thread(
        self,
        user_id: str,
        file_path: str,
        title: str,
        thread_ts: str = None,
        channel_id: str = None,
    ) -> bool:
        """Upload a file and send it as a document/image message."""
        to = channel_id or user_id
        media_id = await self._upload_media(file_path)
        if not media_id:
            return False
        mime, _ = mimetypes.guess_type(file_path)
        mime = mime or "application/octet-stream"
        is_image = mime.startswith("image/")
        msg_type = "image" if is_image else "document"
        media_obj: Dict[str, Any] = {"id": media_id, "caption": title[:1024]}
        if not is_image:
            media_obj["filename"] = os.path.basename(file_path)
        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": msg_type,
            msg_type: media_obj,
        }
        if thread_ts:
            payload["context"] = {"message_id": thread_ts}
        return await self._post_message(payload)

    async def send_image_in_dm(self, user_id: str, image_path: str, title: str) -> bool:
        return await self.send_file_in_thread(user_id, image_path, title)

    async def send_file_in_dm(self, user_id: str, file_path: str, title: str) -> bool:
        return await self.send_file_in_thread(user_id, file_path, title)


# Minimal mapping so Slack-style emoji names used elsewhere work on WhatsApp.
_SLACK_EMOJI_MAP = {
    "eyes": "\U0001F440",          # 👀
    "white_check_mark": "\u2705",  # ✅
    "x": "\u274C",                 # ❌
    "warning": "\u26A0\ufe0f",     # ⚠️
    "hourglass": "\u23F3",         # ⏳
}
