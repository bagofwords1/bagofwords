"""Data Edge Agent — the process that runs on the customer's site.

It holds the credentials for its local data sources, constructs real clients
against them, and answers proxied client operations that arrive over NATS. The
Bow instance never obtains those credentials; it sends the call, not the login.

Phase 1 is the skeleton: configuration, start-up, and the NATS connection —
subscribing to this agent's subjects and logging what arrives. Dispatch to real
clients lands in a later phase.
"""

__version__ = "0.1.0"
