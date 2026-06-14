"""Poller loop: routes authentic mail, drops spoof/loop/dupes — no DB/IMAP."""
from app.services.email.mailbox_reader import FakeMailboxReader
from app.services.email_poller_service import EmailPoller, _processed_message_ids


def _raw(from_addr, msg_id, dmarc="pass", subject="Q", extra_headers=""):
    return (
        f"From: {from_addr}\n"
        f"To: analyst@bow.test\n"
        f"Subject: {subject}\n"
        f"Message-ID: <{msg_id}>\n"
        f"Authentication-Results: mx; dmarc={dmarc}\n"
        f"{extra_headers}"
        f"\nbody text\n"
    ).encode()


async def _collect_handler(store):
    async def handler(event_data):
        store.append(event_data)
    return handler


async def test_authentic_message_is_routed():
    _processed_message_ids.clear()
    reader = FakeMailboxReader()
    reader.deliver(_raw("alice@acme.com", "m1@acme.com"))
    routed = []

    poller = EmailPoller(
        reader,
        handler=(await _collect_handler(routed)),
        allowed_domains=["acme.com"],
        own_addresses=["analyst@bow.test"],
    )
    summary = await poller.poll_once()

    assert summary["fetched"] == 1
    assert summary["routed"] == 1
    assert len(routed) == 1
    # The raw bytes + security metadata are handed to the manager.
    assert routed[0]["security"]["from_address"] == "alice@acme.com"
    assert b"alice@acme.com" in routed[0]["raw"]
    # Message consumed (marked seen).
    assert await reader.fetch_unseen() == []


async def test_spoofed_message_is_blocked_not_routed():
    _processed_message_ids.clear()
    reader = FakeMailboxReader()
    reader.deliver(_raw("ceo@acme.com", "spoof@x", dmarc="fail"))
    routed = []
    blocked = []

    async def on_blocked(addr, reason):
        blocked.append((addr, reason))

    poller = EmailPoller(
        reader,
        handler=(await _collect_handler(routed)),
        allowed_domains=["acme.com"],
        on_blocked=on_blocked,
    )
    summary = await poller.poll_once()

    assert summary["blocked"] == 1
    assert summary["routed"] == 0
    assert routed == []
    assert blocked and blocked[0][0] == "ceo@acme.com"


async def test_offlist_domain_blocked():
    _processed_message_ids.clear()
    reader = FakeMailboxReader()
    reader.deliver(_raw("mallory@evil.com", "e1@evil.com"))
    routed = []
    poller = EmailPoller(reader, handler=(await _collect_handler(routed)), allowed_domains=["acme.com"])
    summary = await poller.poll_once()
    assert summary["blocked"] == 1
    assert routed == []


async def test_duplicate_message_id_skipped():
    _processed_message_ids.clear()
    reader = FakeMailboxReader()
    reader.deliver(_raw("alice@acme.com", "dup@acme.com"))
    routed = []
    poller = EmailPoller(reader, handler=(await _collect_handler(routed)), allowed_domains=["acme.com"])

    await poller.poll_once()
    # Same Message-ID arrives again (e.g. re-fetch) -> skipped.
    reader.deliver(_raw("alice@acme.com", "dup@acme.com"))
    summary = await poller.poll_once()
    assert summary["skipped"] == 1
    assert len(routed) == 1
