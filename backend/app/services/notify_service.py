"""NotifyService — deliver a free-form message to org members across channels.

This is the channel-agnostic generalization of ``send_email``. The agent (or a
scheduled run) hands it a subject/body, optional report-scoped attachments, and a
set of recipient **emails**; the service:

1. **Resolves + authorizes recipients** against the org's ``memberships`` — only
   active members or pending invites are accepted, never free-form outsiders.
   The requesting user (self) is ALWAYS added, so the sender keeps a copy.
2. **Always delivers in-app** via :class:`InboxService` (the per-user inbox from
   the notification center) for every recipient that has a user account.
3. **Adds one external nudge per recipient**: the recipient's preferred verified
   chat platform (Teams → Slack) if reachable, else email. Never both — in-app is
   the always-on layer, the external nudge is a one-way pointer back into the app.

The membership check is the security boundary; it lives here so the internal
``notify`` tool and the external MCP path share exactly one implementation.
Reply-threading (a recipient replying re-attaches to a report) is kept owner-only
by only passing ``system_completion`` through for the self recipient.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import SOURCE_REPORT_TOOL

logger = logging.getLogger(__name__)

# Preferred chat platforms for the external nudge, in order. Email is the
# universal fallback when none of these is reachable for a recipient.
_CHAT_PLATFORMS = ("teams", "slack")

_TAG_RE = re.compile(r"<[^>]+>")


def _snippet(body: str, body_format: str, limit: int = 280) -> str:
    """A short plain-text preview of the body for the in-app row."""
    text = body or ""
    if body_format == "html":
        text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


@dataclass
class ResolvedRecipient:
    email: str
    user_id: Optional[str] = None  # None for a pending invite with no account yet
    is_self: bool = False
    name: Optional[str] = None


@dataclass
class RecipientDelivery:
    email: str
    name: Optional[str] = None
    is_self: bool = False
    delivered: List[str] = field(default_factory=list)  # channels that succeeded
    failed: List[str] = field(default_factory=list)      # channels that failed
    error: Optional[str] = None


class NotifyService:
    """Resolve recipients and fan a message out across in-app + one external channel."""

    async def resolve_recipients(
        self,
        db: AsyncSession,
        organization: Any,
        sender: Any,
        emails: List[str],
    ) -> Tuple[List[ResolvedRecipient], List[str]]:
        """Validate ``emails`` against the org's memberships and always include self.

        Returns ``(resolved, rejected)``. ``rejected`` holds any requested address
        that does not belong to an active member or pending invite in this org —
        the caller surfaces it but it is never delivered to. Self is always first.
        """
        from app.models.membership import Membership
        from app.models.user import User

        org_id = str(getattr(organization, "id", "") or "")
        resolved: List[ResolvedRecipient] = []
        seen: set[str] = set()

        self_email = (getattr(sender, "email", None) or "").strip().lower()
        if self_email:
            resolved.append(ResolvedRecipient(
                email=self_email,
                user_id=str(sender.id),
                is_self=True,
                name=getattr(sender, "name", None),
            ))
            seen.add(self_email)

        wanted = [(e or "").strip().lower() for e in (emails or [])]
        wanted = [e for e in wanted if e and e not in seen]
        if not wanted:
            return resolved, []

        # Build an email → (user_id, name) map for the org's memberships. Active
        # members are keyed by their user's email; pending invites by the
        # membership's own email (no account yet).
        rows = (await db.execute(
            select(Membership, User)
            .outerjoin(User, Membership.user_id == User.id)
            .where(Membership.organization_id == org_id)
        )).all()
        member_map: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        for m, u in rows:
            if u is not None and u.email:
                member_map[u.email.strip().lower()] = (str(u.id), getattr(u, "name", None))
            if m.email:
                member_map.setdefault(m.email.strip().lower(), (str(u.id) if u else None, None))

        rejected: List[str] = []
        for email in wanted:
            if email in seen:
                continue
            hit = member_map.get(email)
            if hit is None:
                rejected.append(email)
                continue
            user_id, name = hit
            resolved.append(ResolvedRecipient(email=email, user_id=user_id, is_self=False, name=name))
            seen.add(email)

        return resolved, rejected

    async def notify(
        self,
        db: AsyncSession,
        *,
        sender: Any,
        organization: Any,
        report: Any = None,
        subject: str,
        body: str,
        body_format: str = "text",
        attachment_specs: Optional[List[Any]] = None,
        recipient_emails: Optional[List[str]] = None,
        source: str = SOURCE_REPORT_TOOL,
        system_completion: Any = None,
    ) -> Dict[str, Any]:
        """Deliver to all resolved recipients. Returns per-recipient channel results."""
        attachment_specs = attachment_specs or []
        resolved, rejected = await self.resolve_recipients(
            db, organization, sender, recipient_emails or []
        )

        org_id = str(getattr(organization, "id", "") or "")
        report_id = str(report.id) if report is not None and getattr(report, "id", None) else None
        link = f"/reports/{report_id}" if report_id else None
        source_id = str(getattr(system_completion, "id", None)) if system_completion is not None else None

        # ---- in-app: one row per recipient that has an account ----
        inapp_user_ids = [r.user_id for r in resolved if r.user_id]
        inapp_ok: set[str] = set()
        if inapp_user_ids:
            try:
                from app.services.inbox_service import inbox_service

                await inbox_service.notify_users(
                    db,
                    organization_id=org_id,
                    user_ids=inapp_user_ids,
                    source=source,
                    type="notify",
                    title=subject,
                    body=_snippet(body, body_format),
                    link=link,
                    subject={
                        "kind": "report",
                        "report_id": report_id,
                        "attachments": [
                            {"ref_type": a.ref_type, "ref_id": a.ref_id} for a in attachment_specs
                        ],
                    },
                    # actor_user_id is intentionally None so the sender (self) also
                    # receives the in-app copy — notify_users excludes the actor.
                    actor_user_id=None,
                    source_id=source_id,
                    group_key=f"notify:{source_id or report_id or ''}:{(subject or '')[:60]}",
                )
                inapp_ok = set(inapp_user_ids)
            except Exception:  # noqa: BLE001
                logger.exception("notify: in-app delivery failed")

        # ---- external nudge: one channel per recipient ----
        deliveries: List[RecipientDelivery] = []
        for r in resolved:
            d = RecipientDelivery(email=r.email, name=r.name, is_self=r.is_self)
            if r.user_id and r.user_id in inapp_ok:
                d.delivered.append("in_app")

            # Reply-threading stays owner-only: only the self recipient carries the
            # system_completion (so the owner's reply re-attaches to their report).
            sc = system_completion if r.is_self else None
            channel, ok, err = await self._external_nudge(
                db, organization, report, r, subject, body, body_format, attachment_specs, sc
            )
            if channel:
                (d.delivered if ok else d.failed).append(channel)
                if not ok and err:
                    d.error = err
            deliveries.append(d)

        success = any(d.delivered for d in deliveries)
        return {
            "success": success,
            "results": [
                {
                    "email": d.email,
                    "name": d.name,
                    "is_self": d.is_self,
                    "delivered": d.delivered,
                    "failed": d.failed,
                    "error": d.error,
                }
                for d in deliveries
            ],
            "rejected": rejected,
        }

    # ---- internal: pick + send one external channel for a recipient ----

    async def _external_nudge(
        self, db, organization, report, recipient: ResolvedRecipient,
        subject, body, body_format, attachment_specs, system_completion,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """Try the recipient's preferred verified chat platform, else email.

        Returns ``(channel, ok, error)``. ``channel`` is None only if nothing was
        attempted (e.g. no email address at all, which shouldn't happen).
        """
        org_id = str(getattr(organization, "id", "") or "")

        # Chat platforms need an account + a verified mapping; invites (no user)
        # skip straight to email.
        if recipient.user_id:
            for ptype in _CHAT_PLATFORMS:
                try:
                    ch = await self._try_chat(
                        db, org_id, ptype, report, recipient, body, body_format, attachment_specs
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("notify: %s nudge failed for %s", ptype, recipient.email)
                    ch = None
                if ch is not None:
                    return ptype, ch, None

        return await self._send_email(
            db, organization, report, recipient, subject, body, body_format,
            attachment_specs, system_completion,
        )

    async def _try_chat(
        self, db, org_id, ptype, report, recipient, body, body_format, attachment_specs
    ) -> Optional[bool]:
        """Send a DM on ``ptype`` if the org has it active and the recipient has a
        verified mapping. Returns the send result, or None if not reachable here
        (so the caller falls through to the next channel / email)."""
        from app.services.external_platform_service import ExternalPlatformService
        from app.services.external_user_mapping_service import ExternalUserMappingService
        from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory

        platform = await ExternalPlatformService().get_platform_by_type(db, org_id, ptype)
        if not platform or not getattr(platform, "is_active", False):
            return None
        mapping = await ExternalUserMappingService().get_mapping_by_app_user(
            db, org_id, ptype, recipient.user_id
        )
        if not mapping or not mapping.is_verified or not mapping.external_user_id:
            return None

        adapter = PlatformAdapterFactory.create_adapter(platform)
        text = self._with_link(body, body_format, report, plain=True)
        ok = await adapter.send_dm(mapping.external_user_id, text)

        # Best-effort attachments — never fail the message over an attachment.
        if ok and attachment_specs:
            files, temps = await self._resolve_files(db, report, None, attachment_specs)
            try:
                for path, filename in files:
                    try:
                        await adapter.send_file_in_dm(mapping.external_user_id, path, filename)
                    except Exception:  # noqa: BLE001
                        logger.exception("notify: %s attachment send failed", ptype)
            finally:
                self._cleanup(temps)
        return bool(ok)

    async def _send_email(
        self, db, organization, report, recipient, subject, body, body_format,
        attachment_specs, system_completion,
    ) -> Tuple[str, bool, Optional[str]]:
        from app.services.email_send_service import EmailSendService

        try:
            out = await EmailSendService().send(
                db,
                recipient=recipient.email,
                subject=subject,
                body=body,
                body_format=body_format,
                attachment_specs=attachment_specs,
                report=report,
                organization=organization,
                system_completion=system_completion,
            )
            return "email", bool(out.success), (None if out.success else out.error)
        except Exception as e:  # noqa: BLE001
            logger.exception("notify: email send failed for %s", recipient.email)
            return "email", False, str(e)

    async def _resolve_files(self, db, report, organization, specs) -> Tuple[List[Tuple[str, str]], List[str]]:
        """Reuse EmailSendService's report-scoped attachment resolution to produce
        on-disk files for a chat upload. Returns ([(path, filename)], temp_paths)."""
        from app.services.email_send_service import EmailSendService

        svc = EmailSendService()
        files: List[Tuple[str, str]] = []
        temps: List[str] = []
        for spec in specs:
            try:
                res, att_dict, temp_path = await svc.resolve_attachment(spec, db, report, organization)
            except Exception:  # noqa: BLE001
                logger.exception("notify: attachment resolution failed")
                continue
            if att_dict and res.success:
                path = att_dict.get("file")
                if path:
                    files.append((path, res.filename or os.path.basename(path)))
                if temp_path:
                    temps.append(temp_path)
        return files, temps

    @staticmethod
    def _cleanup(paths: List[str]) -> None:
        for p in paths:
            try:
                os.unlink(p)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _with_link(body: str, body_format: str, report: Any, plain: bool) -> str:
        rid = getattr(report, "id", None) if report is not None else None
        if not rid:
            return body
        try:
            from app.settings.config import settings
            base = settings.bow_config.base_url
        except Exception:  # noqa: BLE001
            base = ""
        url = f"{base}/reports/{rid}"
        return f"{body}\n\n{url}"


notify_service = NotifyService()
