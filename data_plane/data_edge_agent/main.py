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
import signal
from typing import Optional

from .config import AgentConfig, load_config
from .logging_setup import configure_logging
from .tunnel import EdgeAgentTunnel

logger = logging.getLogger(__name__)


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


async def run(config: AgentConfig) -> None:
    """Connect, subscribe, and stay up until asked to stop."""
    tunnel = EdgeAgentTunnel(config)
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

    logger.info(
        "edge_agent.started",
        extra={
            "edge_agent_id": config.edge_agent_id,
            "edge_agent_name": config.edge_agent_name,
            "org_id": config.org_id,
            "connections": [c.name for c in config.connections],
        },
    )

    await stop.wait()
    logger.info("edge_agent.stopping")
    advertiser.cancel()
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

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception:
        logger.exception("edge_agent.fatal")
        return 1
    return 0
