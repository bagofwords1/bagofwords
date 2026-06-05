"""Email-channel building blocks.

This subpackage holds the transport-agnostic pieces of the email integration:

- ``security``        inbound spoofing / loop / allowlist evaluation
- ``message_builder`` RFC 5322 message construction with threading headers
- ``sender``          outbound SMTP transport (aiosmtplib)
- ``mailbox_reader``  inbound mailbox transport (IMAP) + a fake for tests

The conversational adapter (``app.services.platform_adapters.email_adapter``)
and the poller (``app.services.email_poller_service``) compose these so the
``ExternalPlatformManager`` can drive email exactly like Slack/Teams/WhatsApp.
"""
