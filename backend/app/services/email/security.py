"""Inbound email security evaluation.

The email channel is a data-exfiltration surface: anything that convinces the
analyst it's a legitimate org user gets the agent to run queries and email the
results back. The ``From`` header is trivially forgeable, so we never trust it
on its own. This module turns a raw inbound message into an
``InboundVerdict`` that the poller uses to decide whether to route the message
to the agent.

Layered controls (see docs/design/email-integration.md):

1. The mailbox itself should be restricted to internal senders (admin config).
2. Trust the receiving provider's anti-spoof verdict — parse the
   ``Authentication-Results`` header it stamped (DMARC / DKIM / SPF). We never
   re-implement the crypto; we read the verdict the provider already computed.
3. Domain allowlist — only senders whose domain is configured are considered.
4. Loop / auto-reply suppression so we never ping-pong with mailers or our own
   outbound mail.

Identity (mapping the verified address to an existing org member, and the
"no auto-provision from email" rule) is enforced downstream in the
``ExternalPlatformManager``; this module is purely about whether the message is
authentic and wanted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.message import Message
from email.utils import parseaddr
from typing import List, Optional


# Headers that indicate the message was machine-generated (vacation responders,
# mailing lists, delivery notifications). Routing these to the agent risks
# infinite loops, so they are always dropped.
_AUTO_HEADER_CHECKS = (
    ("auto-submitted", lambda v: v.strip().lower() != "no"),
    ("precedence", lambda v: v.strip().lower() in {"bulk", "list", "junk", "auto_reply"}),
    ("x-autoreply", lambda v: True),
    ("x-autorespond", lambda v: True),
    ("x-auto-response-suppress", lambda v: True),
    ("list-id", lambda v: True),
    ("list-unsubscribe", lambda v: True),
)


@dataclass
class InboundVerdict:
    """Outcome of evaluating an inbound message."""

    allowed: bool
    reason: str = ""
    from_address: str = ""
    from_domain: str = ""
    dmarc: str = "none"
    dkim: str = "none"
    spf: str = "none"
    is_auto: bool = False
    # Structured metadata persisted alongside the completion for audit.
    metadata: dict = field(default_factory=dict)

    def as_metadata(self) -> dict:
        """Audit-friendly snapshot of the security decision."""
        return {
            "from_address": self.from_address,
            "from_domain": self.from_domain,
            "dmarc": self.dmarc,
            "dkim": self.dkim,
            "spf": self.spf,
            "is_auto": self.is_auto,
            "allowed": self.allowed,
            "reason": self.reason,
            **self.metadata,
        }


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def extract_from_address(msg: Message) -> str:
    """Return the bare ``local@domain`` from the From header (no display name)."""
    _name, addr = parseaddr(msg.get("From", ""))
    return _norm(addr)


def domain_of(address: str) -> str:
    address = _norm(address)
    return address.rsplit("@", 1)[1] if "@" in address else ""


def parse_authentication_results(msg: Message) -> dict:
    """Read the provider-stamped ``Authentication-Results`` verdict.

    Returns a dict like ``{"dmarc": "pass", "dkim": "pass", "spf": "fail"}``.
    Methods not mentioned default to ``"none"``. Multiple headers (one per hop)
    are merged with a "best result wins" rule so a downstream gateway that
    re-stamped ``pass`` isn't masked by an earlier ``none``.
    """
    results = {"dmarc": "none", "dkim": "none", "spf": "none"}
    headers = msg.get_all("Authentication-Results") or []
    rank = {"none": 0, "neutral": 1, "temperror": 1, "permerror": 1, "softfail": 2, "fail": 3, "pass": 4}
    for header in headers:
        for method in ("dmarc", "dkim", "spf"):
            # e.g. "dmarc=pass (p=reject ...)" — capture the token after '='.
            m = re.search(rf"\b{method}\s*=\s*([a-zA-Z]+)", header, re.IGNORECASE)
            if not m:
                continue
            value = m.group(1).lower()
            if rank.get(value, 0) >= rank.get(results[method], 0):
                results[method] = value
    return results


def is_auto_or_loop(msg: Message, own_addresses: Optional[List[str]] = None) -> bool:
    """True if the message is auto-generated or originates from our own mailbox."""
    own = {_norm(a) for a in (own_addresses or []) if a}
    sender = extract_from_address(msg)
    if sender and sender in own:
        return True
    for header, predicate in _AUTO_HEADER_CHECKS:
        value = msg.get(header)
        if value is not None and predicate(value):
            return True
    return False


def evaluate_inbound(
    msg: Message,
    *,
    allowed_domains: Optional[List[str]] = None,
    own_addresses: Optional[List[str]] = None,
    require_auth_pass: bool = True,
) -> InboundVerdict:
    """Decide whether an inbound message may be routed to the agent.

    ``allowed_domains`` — if non-empty, the sender's domain must be in this list.
        An empty/None list means "no domain restriction" (rely on the mailbox
        being internal-only + auth checks). Configured per integration.
    ``own_addresses`` — addresses that are "us" (the analyst mailbox), used for
        loop suppression.
    ``require_auth_pass`` — when True, require the provider verdict to clear:
        ``dmarc=pass`` OR (``dkim=pass`` and ``spf`` not failing). Set False only
        for trusted internal relays that don't stamp Authentication-Results.
    """
    from_address = extract_from_address(msg)
    from_domain = domain_of(from_address)
    auth = parse_authentication_results(msg)
    is_auto = is_auto_or_loop(msg, own_addresses)

    def verdict(allowed: bool, reason: str) -> InboundVerdict:
        return InboundVerdict(
            allowed=allowed,
            reason=reason,
            from_address=from_address,
            from_domain=from_domain,
            dmarc=auth["dmarc"],
            dkim=auth["dkim"],
            spf=auth["spf"],
            is_auto=is_auto,
        )

    if not from_address:
        return verdict(False, "missing_from_address")

    if is_auto:
        return verdict(False, "auto_or_loop")

    if require_auth_pass:
        dmarc_ok = auth["dmarc"] == "pass"
        dkim_aligned = auth["dkim"] == "pass" and auth["spf"] != "fail"
        if not (dmarc_ok or dkim_aligned):
            return verdict(False, f"auth_failed(dmarc={auth['dmarc']},dkim={auth['dkim']},spf={auth['spf']})")

    if allowed_domains:
        normalized = {_norm(d).lstrip("@") for d in allowed_domains if d}
        if from_domain not in normalized:
            return verdict(False, f"domain_not_allowed({from_domain})")

    return verdict(True, "ok")
