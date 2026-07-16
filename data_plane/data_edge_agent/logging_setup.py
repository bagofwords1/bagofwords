"""Logging for the agent.

Matches the backend's convention — `logging.getLogger(__name__)`, dotted event
names, structured detail in `extra` — so the two read the same way when someone
is correlating a control-plane trace against an agent log.
"""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Everything the stdlib puts on a LogRecord itself. Anything else came from an
# `extra=` and is ours to render.
_STANDARD = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class _ExtraFormatter(logging.Formatter):
    """Render `extra=` fields, which the default formatter silently drops.

    The backend logs structured detail via `extra` because its handlers are
    structured. This agent logs to stdout in someone else's data centre, where
    a bare "edge_agent.nats.error" with the reason discarded is the difference
    between a five-second fix and a support ticket.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _STANDARD and not k.startswith("_")
        }
        if not extras:
            return base
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
        return f"{base} [{rendered}]"


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ExtraFormatter(_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # nats-py logs every reconnect attempt at INFO, which is noise when a
    # broker is down and the agent is retrying forever by design.
    logging.getLogger("nats").setLevel(logging.WARNING)
