"""Entry point for the data edge agent.

    python -m data_plane.data_edge_agent --config config.yaml

Start-up is: load config, configure logging, connect to NATS, subscribe, then
wait. Shutdown drains the connection so in-flight replies land before the socket
goes away.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Optional

from .admin.server import AdminServer
from .audit import AuditLog
from .config import AgentConfig, load_config
from .logging_setup import configure_logging
from .store import ConnectionStore
from .tunnel import EdgeAgentTunnel

logger = logging.getLogger(__name__)

_DEFAULT_STORE_NAME = "edge-agent-store.json"
_DEFAULT_AUDIT_NAME = "edge-agent-audit.jsonl"


def _resolve_store_path(config: AgentConfig, config_path: Optional[str]) -> Path:
    """Where the credential store lives.

    Explicit `store_path` wins. Otherwise it sits beside the config file so the
    two travel together, falling back to the CWD when the config came only from
    the environment (no file to sit beside).
    """
    if config.store_path:
        return Path(config.store_path)
    if config_path:
        return Path(config_path).resolve().parent / _DEFAULT_STORE_NAME
    return Path.cwd() / _DEFAULT_STORE_NAME


def _resolve_audit_path(config: AgentConfig, config_path: Optional[str]) -> Path:
    """Where the audit trail (JSONL) lives — same rule as the store."""
    if config.audit_path:
        return Path(config.audit_path)
    if config_path:
        return Path(config_path).resolve().parent / _DEFAULT_AUDIT_NAME
    return Path.cwd() / _DEFAULT_AUDIT_NAME


def _merge_store_connections(config: AgentConfig, store: ConnectionStore) -> None:
    """Fold UI-managed connections into the config, store winning by name.

    File-defined connections still load, but an admin-UI edit to the same name
    lives in the store and takes precedence — the store is the source of truth
    for anything the UI can change.
    """
    stored = {c.name: c for c in store.list()}
    if not stored:
        return
    merged = [stored.pop(c.name, c) for c in config.connections]
    merged.extend(stored.values())  # store-only connections
    config.connections = merged


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="data_edge_agent",
        description="Bow data edge agent — serves local data sources to a Bow instance over NATS.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to the YAML config file (or set BOW_EDGE_AGENT_CONFIG)",
    )
    return parser.parse_args(argv)


async def run(config: AgentConfig, store: Optional[ConnectionStore] = None,
              audit: Optional[AuditLog] = None) -> None:
    """Connect, subscribe, and stay up until asked to stop."""
    tunnel = EdgeAgentTunnel(config, audit=audit)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler beats signal.signal here: it wakes the loop rather
        # than running the handler on whatever frame happened to be executing.
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: stop.set())

    # connect() owns its own retry loop and takes the stop event, so shutting
    # down while the broker is unreachable needs no task cancellation — see
    # EdgeAgentTunnel.connect for why cancelling one is the thing to avoid.
    await tunnel.connect(stop=stop)

    if stop.is_set():
        logger.info("edge_agent.stopping", extra={"during": "connect"})
        await tunnel.close()
        logger.info("edge_agent.stopped")
        return

    await tunnel.subscribe()

    # Advertise immediately so a control plane that is already up registers
    # this agent now, then keep re-publishing (see advertise_forever).
    await tunnel.advertise()
    advertiser = asyncio.create_task(tunnel.advertise_forever(stop))
    # Heartbeat for UI liveness (A10): a missed window flips the agent's status
    # and deactivates its connections on the control plane.
    heartbeater = asyncio.create_task(tunnel.heartbeat_forever(stop))

    # Local admin UI (design C4). Only started when both enabled and given a
    # store to persist edits to; loopback-bound, so it is the operator's own
    # window onto this agent and never a remote surface.
    admin: Optional[AdminServer] = None
    if config.admin_enabled and store is not None:
        admin = AdminServer(tunnel, store, config.admin_host, config.admin_port)
        try:
            await admin.start()
        except OSError as e:
            # A busy admin port must not take the whole agent down — the tunnel
            # is the job; the UI is a convenience. Log and carry on without it.
            logger.error(
                "edge_agent.admin.start_failed",
                extra={"host": config.admin_host, "port": config.admin_port, "error": str(e)},
            )
            admin = None

    logger.info(
        "edge_agent.started",
        extra={
            "edge_agent_id": config.edge_agent_id,
            "edge_agent_name": config.edge_agent_name,
            "org_id": config.org_id,
            "connections": [c.name for c in config.connections],
            "admin_ui": (f"http://{config.admin_host}:{config.admin_port}"
                         if admin is not None else None),
        },
    )

    await stop.wait()
    logger.info("edge_agent.stopping")
    advertiser.cancel()
    heartbeater.cancel()
    if admin is not None:
        await admin.stop()
    await tunnel.close()
    logger.info("edge_agent.stopped")


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception as e:
        # Logging is not configured yet, and a config error is the one failure
        # an operator will hit before anything else works.
        print(f"data edge agent: configuration error: {e}")
        return 2

    configure_logging(config.log_level)

    # Build the credential store and fold its connections in before start-up,
    # so admin-UI-managed sources are served on this boot too. A store failure
    # (e.g. a wrong key) is fatal: serving a connection with silently-missing
    # credentials is worse than refusing to start.
    config_path = args.config or os.environ.get("BOW_EDGE_AGENT_CONFIG")

    # Local audit trail (C4/C5): always on — it is a security feature, not a
    # convenience. File-backed so it survives restarts; a write failure never
    # breaks a request (AuditLog swallows it).
    audit = AuditLog(_resolve_audit_path(config, config_path), config.audit_retain)

    store: Optional[ConnectionStore] = None
    if config.admin_enabled:
        try:
            store = ConnectionStore(_resolve_store_path(config, config_path), config.store_key)
            _merge_store_connections(config, store)
        except Exception:
            logger.exception("edge_agent.store.fatal")
            return 1

    try:
        asyncio.run(run(config, store, audit))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception:
        logger.exception("edge_agent.fatal")
        return 1
    return 0
