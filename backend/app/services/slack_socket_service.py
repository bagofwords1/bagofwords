"""Slack Socket Mode listener.

Slack's outbound-only transport: the app opens a WebSocket to Slack
(`apps.connections.open` with an app-level ``xapp-`` token) and Slack pushes
envelopes whose ``payload`` is the same ``event_callback`` JSON the Events
API webhook receives — no public URL, Request URL, or signing secret needed.
Started from ``main.py`` under the scheduler leader (same lifecycle as the
email poller and the Google Chat Pub/Sub listener); one connection per
Slack workspace with ``connection_mode == "socket_mode"``.

Protocol notes:
- Every envelope must be acked (``{"envelope_id": …}``) promptly; we ack
  BEFORE processing so slow agent turns never trigger Slack redelivery.
- Slack refreshes connections every few hours via a ``disconnect`` envelope;
  the client reconnects to a fresh URL with backoff.
- Event dedupe (Slack redelivers unacked envelopes) lives in
  ``slack_event_service.processed_events``, shared with the webhook route.
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

CONNECTIONS_OPEN_URL = "https://slack.com/api/apps.connections.open"


async def open_socket_url(app_token: str) -> Optional[str]:
    """Mint an ephemeral wss:// URL for this app. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                CONNECTIONS_OPEN_URL,
                headers={"Authorization": f"Bearer {app_token}"},
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                return data.get("url")
            logger.warning("SLACK_SOCKET: apps.connections.open failed: %s", data.get("error"))
            return None
    except Exception as e:  # noqa: BLE001
        logger.warning("SLACK_SOCKET: apps.connections.open error: %s", e)
        return None


class SlackSocketClient:
    """One workspace's Socket Mode connection.

    ``handler`` receives each events_api envelope's payload (the
    ``event_callback`` dict). Transport concerns (ack, reconnect, hello/
    disconnect frames) stay in here so the handler is transport-agnostic.
    """

    def __init__(self, app_token: str, handler: Callable[[dict], Any]):
        self.app_token = app_token
        self.handler = handler
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def process_envelope(self, raw: str) -> tuple[Optional[dict], Optional[dict], bool]:
        """Parse one frame. Returns (ack, event_payload, should_reconnect).

        Pure function of the frame so it's unit-testable without a socket:
        - ack: dict to send back on the wire, or None
        - event_payload: event_callback dict to hand off, or None
        - should_reconnect: True when Slack asked us to refresh
        """
        try:
            envelope = json.loads(raw)
        except Exception:
            return None, None, False

        etype = envelope.get("type")
        if etype == "disconnect":
            return None, None, True
        if etype == "hello":
            return None, None, False

        ack = None
        if envelope.get("envelope_id"):
            ack = {"envelope_id": envelope["envelope_id"]}

        payload = None
        if etype == "events_api":
            payload = envelope.get("payload")

        # Other envelope types (interactive, slash_commands) are acked and
        # dropped — not part of the chat pipeline.
        return ack, payload, False

    async def run_once(self) -> None:
        """One connection lifetime: connect, pump frames until disconnect."""
        import websockets

        url = await open_socket_url(self.app_token)
        if not url:
            raise ConnectionError("could not open socket url")

        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            logger.info("SLACK_SOCKET: connected")
            while not self._stopped:
                raw = await ws.recv()
                ack, payload, reconnect = self.process_envelope(raw)
                if ack:
                    # Ack first: processing may take minutes (agent turn) and
                    # Slack redelivers unacked envelopes after ~3s.
                    await ws.send(json.dumps(ack))
                if payload is not None:
                    try:
                        await self.handler(payload)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("SLACK_SOCKET: handler failed: %s", e)
                if reconnect:
                    logger.info("SLACK_SOCKET: disconnect frame — refreshing connection")
                    return

    async def run(self) -> None:
        """Reconnect loop with backoff, until stop()."""
        backoff = 1.0
        while not self._stopped:
            try:
                await self.run_once()
                backoff = 1.0  # clean refresh — reconnect immediately
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("SLACK_SOCKET: connection error: %s (retry in %.0fs)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


async def run_slack_socket_listeners(
    interval_seconds: float = 5.0, stop_event: Optional[asyncio.Event] = None
) -> None:
    """Production entry point: keep one socket task per socket-mode workspace.

    Discovery loop mirrors the Google Chat listener: picks up added, changed
    and removed integrations without a restart. Heavy app imports deferred so
    the module stays cheap to import in unit tests.
    """
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.external_platform import ExternalPlatform
    from app.services.slack_event_service import handle_slack_event

    clients: Dict[str, SlackSocketClient] = {}
    tasks: Dict[str, asyncio.Task] = {}

    async def _make_handler():
        async def _handler(event_payload: dict) -> None:
            async with async_session_maker() as db:
                await handle_slack_event(db, event_payload)
        return _handler

    handler = await _make_handler()

    while not (stop_event and stop_event.is_set()):
        try:
            async with async_session_maker() as db:
                stmt = select(ExternalPlatform).where(
                    ExternalPlatform.platform_type == "slack",
                    ExternalPlatform.is_active == True,  # noqa: E712
                )
                result = await db.execute(stmt)
                platforms = result.scalars().all()

            active: Dict[str, str] = {}
            for platform in platforms:
                cfg = platform.platform_config or {}
                if cfg.get("connection_mode") != "socket_mode":
                    continue
                creds = platform.decrypt_credentials()
                app_token = creds.get("app_token")
                if app_token:
                    active[str(platform.id)] = app_token

            # Stop clients for removed/deactivated/reconfigured platforms.
            for pid in list(tasks):
                if pid not in active or (
                    pid in clients and clients[pid].app_token != active[pid]
                ):
                    clients.pop(pid, None)
                    task = tasks.pop(pid)
                    task.cancel()

            # Start (or restart crashed) clients.
            for pid, app_token in active.items():
                task = tasks.get(pid)
                if task is None or task.done():
                    client = SlackSocketClient(app_token, handler)
                    clients[pid] = client
                    tasks[pid] = asyncio.create_task(client.run())
                    logger.info("SLACK_SOCKET: listener task started for platform %s", pid)
        except Exception as e:  # noqa: BLE001
            logger.warning("SLACK_SOCKET: discovery cycle failed: %s", e)

        try:
            await asyncio.wait_for(
                stop_event.wait() if stop_event else asyncio.sleep(interval_seconds),
                timeout=interval_seconds,
            )
        except asyncio.TimeoutError:
            pass

    for pid, client in clients.items():
        client.stop()
    for task in tasks.values():
        task.cancel()
