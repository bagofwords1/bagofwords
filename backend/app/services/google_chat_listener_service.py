"""Google Chat Pub/Sub pull listener.

Google Chat's Pub/Sub connection mode delivers events to a topic in the
customer's GCP project; this listener pulls them over an outbound connection
(no public URL, webhook, or inbound port — the same reason the email channel
polls IMAP). Started from ``main.py`` under the scheduler leader via
:func:`run_google_chat_listeners`, and feeds MESSAGE events into the same
``ExternalPlatformManager`` pipeline Slack/Teams webhooks use.

Delivery notes:
- Pub/Sub is at-least-once, so events are deduped by Pub/Sub messageId
  (bounded in-memory set — same tradeoff as the webhook routes' event sets).
- Events are acked after the handler returns (or when skipped), so a crash
  mid-handling redelivers rather than drops.
- The pull request long-polls server-side; a read timeout simply means "no
  messages this cycle".
"""

import asyncio
import base64
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"
PUBSUB_BASE = "https://pubsub.googleapis.com/v1"


class GoogleChatPubSubListener:
    """Pulls one org's Google Chat events from its Pub/Sub subscription."""

    def __init__(
        self,
        subscription: str,
        service_account_info: dict,
        handler: Callable[[dict], Any],
        max_messages: int = 10,
    ):
        self.subscription = subscription.strip().strip("/")
        self.service_account_info = service_account_info
        self.handler = handler
        self.max_messages = max_messages
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        # Pub/Sub messageIds already processed (bounded).
        self._seen_ids: set = set()

    async def _get_token(self) -> Optional[str]:
        if self._token and self._token_expires_at > time.time() + 300:
            return self._token

        def _mint() -> str:
            from google.oauth2 import service_account
            import google.auth.transport.requests

            creds = service_account.Credentials.from_service_account_info(
                self.service_account_info, scopes=[PUBSUB_SCOPE]
            )
            creds.refresh(google.auth.transport.requests.Request())
            return creds.token

        try:
            self._token = await asyncio.to_thread(_mint)
            self._token_expires_at = time.time() + 3300
            return self._token
        except Exception as e:
            logger.warning("GCHAT_LISTENER: token mint failed for %s: %s", self.subscription, e)
            return None

    async def pull_once(self) -> int:
        """One pull + handle + ack cycle. Returns number of handled events."""
        token = await self._get_token()
        if not token:
            return 0

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{PUBSUB_BASE}/{self.subscription}:pull",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"maxMessages": self.max_messages, "returnImmediately": True},
                )
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            return 0
        except Exception as e:
            logger.warning("GCHAT_LISTENER: pull failed for %s: %s", self.subscription, e)
            return 0

        if resp.status_code != 200:
            logger.warning(
                "GCHAT_LISTENER: pull HTTP %s for %s: %s",
                resp.status_code, self.subscription, resp.text[:200],
            )
            return 0

        received = resp.json().get("receivedMessages", [])
        if not received:
            return 0

        handled = 0
        ack_ids = []
        for rm in received:
            ack_ids.append(rm.get("ackId"))
            pm = rm.get("message") or {}
            msg_id = pm.get("messageId")
            if msg_id in self._seen_ids:
                continue
            self._seen_ids.add(msg_id)
            if len(self._seen_ids) > 2000:
                self._seen_ids.clear()

            event = self._decode_event(pm)
            if event is None:
                continue
            # Only MESSAGE events reach the agent; membership events
            # (ADDED_TO_SPACE / REMOVED_FROM_SPACE) are acked and skipped.
            if event.get("type") != "MESSAGE":
                continue
            try:
                await self.handler(event)
                handled += 1
            except Exception as e:
                logger.warning("GCHAT_LISTENER: handler failed: %s", e)

        if ack_ids:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.post(
                        f"{PUBSUB_BASE}/{self.subscription}:acknowledge",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"ackIds": [a for a in ack_ids if a]},
                    )
            except Exception as e:
                logger.warning("GCHAT_LISTENER: ack failed for %s: %s", self.subscription, e)

        return handled

    @staticmethod
    def _decode_event(pubsub_message: dict) -> Optional[dict]:
        """Decode a Pub/Sub message's base64 data into a Chat event dict."""
        try:
            data = base64.b64decode(pubsub_message.get("data") or b"")
            return json.loads(data)
        except Exception:
            # Not a Chat event (e.g. a manual test publish) — skip quietly.
            return None


async def run_google_chat_listeners(
    interval_seconds: float = 3.0, stop_event: Optional[asyncio.Event] = None
) -> None:
    """Production entry point: discover active google_chat platforms and pull.

    Heavy app imports are deferred so importing this module stays cheap for
    unit tests (mirrors ``run_email_pollers``). Listener instances are kept
    per platform id so token caches and dedupe sets survive across cycles.
    """
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.external_platform import ExternalPlatform
    from app.services.external_platform_manager import ExternalPlatformManager

    manager = ExternalPlatformManager()
    listeners: Dict[str, GoogleChatPubSubListener] = {}

    while not (stop_event and stop_event.is_set()):
        try:
            async with async_session_maker() as db:
                stmt = select(ExternalPlatform).where(
                    ExternalPlatform.platform_type == "google_chat",
                    ExternalPlatform.is_active == True,  # noqa: E712
                )
                result = await db.execute(stmt)
                platforms = result.scalars().all()

            active_ids = set()
            for platform in platforms:
                cfg = platform.platform_config or {}
                subscription = cfg.get("pubsub_subscription")
                if not subscription:
                    continue
                platform_id = str(platform.id)
                active_ids.add(platform_id)

                # Recreate the listener if the subscription was reconfigured.
                existing = listeners.get(platform_id)
                if existing and existing.subscription != subscription.strip().strip("/"):
                    listeners.pop(platform_id, None)

                if platform_id not in listeners:
                    creds = platform.decrypt_credentials()
                    sa_info = creds.get("service_account_json")
                    if isinstance(sa_info, str):
                        try:
                            sa_info = json.loads(sa_info)
                        except Exception:
                            sa_info = None
                    if not sa_info:
                        continue
                    org_id = platform.organization_id

                    async def _handler(event_data: dict, _org_id=org_id) -> None:
                        async with async_session_maker() as handler_db:
                            await manager.handle_incoming_message(
                                handler_db, "google_chat", _org_id, event_data
                            )

                    listeners[platform_id] = GoogleChatPubSubListener(
                        subscription=subscription,
                        service_account_info=sa_info,
                        handler=_handler,
                    )

            # Drop listeners for deleted/deactivated platforms.
            for stale_id in set(listeners) - active_ids:
                listeners.pop(stale_id, None)

            for listener in listeners.values():
                await listener.pull_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("GCHAT_LISTENER: discovery cycle failed: %s", e)

        try:
            await asyncio.wait_for(
                stop_event.wait() if stop_event else asyncio.sleep(interval_seconds),
                timeout=interval_seconds,
            )
        except asyncio.TimeoutError:
            pass
