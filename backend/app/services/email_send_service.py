"""Shared service for sending free-form emails with report-scoped attachments.

Both the internal agent tool (``app.ai.tools.implementations.send_email``) and
the external MCP tool (``app.ai.tools.mcp.send_email``) delegate here so the
security-sensitive attachment-scoping logic lives in exactly one place.

Attachments are generated on the fly from objects the assistant already
produced — a visualization/query result (CSV/XLSX), an artifact (PPTX for
slides, PDF for pages), or an uploaded file (as-is). Every reference is checked
against the supplied report / organization, so a caller can only attach objects
belonging to that scope; it can never exfiltrate arbitrary objects.
"""

from typing import Any, Dict, List, Optional, Tuple
from io import BytesIO
import os
import re
import tempfile
import logging

from app.ai.tools.schemas.send_email import (
    EmailAttachmentSpec,
    SendEmailAttachmentResult,
    SendEmailOutput,
)

logger = logging.getLogger(__name__)


# Default export format per ref_type when the caller doesn't specify one.
_DEFAULT_FORMAT = {
    "visualization": "csv",
    "query": "csv",
    "artifact": None,  # resolved from artifact.mode (slides -> pptx, page -> pdf)
    "file": None,      # original format
}

# MIME (maintype, subtype) per export format, in the fastapi-mail dict shape.
_MIME = {
    "csv": ("text", "csv"),
    "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "pptx": ("application", "vnd.openxmlformats-officedocument.presentationml.presentation"),
    "pdf": ("application", "pdf"),
}


def _slugify(name: str) -> str:
    name = (name or "attachment").strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return name or "attachment"


def _content_disposition(filename: str) -> str:
    """Build a Content-Disposition value that displays ``filename`` reliably.

    Plain ``filename="..."`` for ASCII names; RFC 2231 ``filename*`` for names
    with non-ASCII characters so mail clients render them correctly.
    """
    try:
        filename.encode("ascii")
        safe = filename.replace('"', "")
        return f'attachment; filename="{safe}"'
    except UnicodeEncodeError:
        from urllib.parse import quote
        return f"attachment; filename*=UTF-8''{quote(filename)}"


def _att_dict(path: str, filename: str, maintype: str, subtype: str) -> Dict[str, Any]:
    """Build a fastapi-mail attachment dict that sets BOTH the MIME type and the
    displayed filename.

    This fastapi-mail version reads ``mime_type``/``mime_subtype`` for the part
    type and otherwise derives the filename from the on-disk basename — so the
    displayed name is controlled via an explicit Content-Disposition header.
    """
    return {
        "file": path,
        "mime_type": maintype,
        "mime_subtype": subtype,
        "headers": {"Content-Disposition": _content_disposition(filename)},
    }


def _write_temp(content: bytes, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="bow_email_attach_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
    except Exception:
        try:
            os.unlink(path)
        except Exception:
            pass
        raise
    return path


class EmailSendService:
    """Resolve report-scoped attachments and send a free-form email."""

    async def send(
        self,
        db,
        *,
        recipient: str,
        subject: str,
        body: str,
        body_format: str = "text",
        attachment_specs: Optional[List[EmailAttachmentSpec]] = None,
        report: Any = None,
        organization: Any = None,
    ) -> SendEmailOutput:
        """Send an email to ``recipient`` with the given attachments.

        Attachments are resolved against ``report`` / ``organization`` for
        scoping. Attachment failures are reported per-item but never block the
        send. Returns a :class:`SendEmailOutput`.
        """
        attachment_specs = attachment_specs or []

        temp_paths: List[str] = []
        attachment_dicts: List[Dict[str, Any]] = []
        attachment_results: List[SendEmailAttachmentResult] = []
        try:
            for spec in attachment_specs:
                result, att_dict, temp_path = await self.resolve_attachment(
                    spec, db, report, organization
                )
                attachment_results.append(result)
                if att_dict:
                    attachment_dicts.append(att_dict)
                if temp_path:
                    temp_paths.append(temp_path)

            from app.services.notification_service import notification_service

            subtype = "html" if body_format == "html" else "plain"
            send_result = await notification_service.send_custom_email(
                recipients=[recipient],
                subject=subject,
                body=body,
                subtype=subtype,
                attachments=attachment_dicts or None,
            )

            success = send_result.status == "sent"
            return SendEmailOutput(
                success=success,
                recipient=recipient,
                subject=subject,
                attachments=attachment_results,
                error=None if success else (send_result.error or "Failed to send email"),
            )
        finally:
            # Only remove temp files we created; never touch persistent paths
            # (uploaded files, generated artifact PDFs in uploads/).
            for p in temp_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass

    # ---- attachment resolution ----

    async def resolve_attachment(
        self, spec: EmailAttachmentSpec, db, report: Any, organization: Any
    ) -> Tuple[SendEmailAttachmentResult, Optional[Dict[str, Any]], Optional[str]]:
        """Resolve a single attachment spec to a fastapi-mail attachment dict.

        Returns (result, attachment_dict_or_None, temp_path_to_cleanup_or_None).
        On any failure, result.success is False with an error and the dict is None.
        """
        res = SendEmailAttachmentResult(ref_type=spec.ref_type, ref_id=spec.ref_id, success=False)
        try:
            if spec.ref_type in ("visualization", "query"):
                return await self._resolve_tabular(spec, db, report, organization, res)
            if spec.ref_type == "artifact":
                return await self._resolve_artifact(spec, db, report, res)
            if spec.ref_type == "file":
                return await self._resolve_file(spec, db, organization, res)
            res.error = f"Unknown ref_type '{spec.ref_type}'"
            return res, None, None
        except Exception as e:
            logger.exception("Attachment resolution failed for %s %s", spec.ref_type, spec.ref_id)
            res.error = str(e)
            return res, None, None

    @staticmethod
    def _scope_ids(report: Any, organization: Any) -> Tuple[Optional[str], Optional[str]]:
        report_id = str(getattr(report, "id", "")) if report is not None else None
        org_id = str(getattr(organization, "id", "")) if organization is not None else None
        return report_id, org_id

    async def _resolve_tabular(self, spec, db, report, organization, res):
        """visualization_id / query_id -> CSV or XLSX of the underlying step data."""
        from sqlalchemy import select
        from app.models.visualization import Visualization
        from app.models.query import Query
        from app.models.step import Step
        from app.services.step_service import StepService

        report_id, org_id = self._scope_ids(report, organization)

        query: Optional[Query] = None
        title = None
        if spec.ref_type == "visualization":
            viz = await db.get(Visualization, spec.ref_id)
            if not viz:
                res.error = "Visualization not found"
                return res, None, None
            if report_id and str(viz.report_id) != report_id:
                res.error = "Visualization does not belong to this report"
                return res, None, None
            title = viz.title
            query = await db.get(Query, viz.query_id) if viz.query_id else None
        else:  # query
            query = await db.get(Query, spec.ref_id)
            if not query:
                res.error = "Query not found"
                return res, None, None
            # Queries may be report-scoped or global-to-org; require one to match.
            q_report = str(query.report_id) if query.report_id else None
            q_org = str(query.organization_id) if query.organization_id else None
            if report_id and q_report and q_report != report_id and (not org_id or q_org != org_id):
                res.error = "Query does not belong to this report or organization"
                return res, None, None
            title = query.title

        if not query:
            res.error = "No query linked to this visualization"
            return res, None, None

        # Find the step that holds the result: default step, else latest.
        step = None
        if query.default_step_id:
            step = await db.get(Step, query.default_step_id)
        if not step:
            step = (await db.execute(
                select(Step).where(Step.query_id == query.id).order_by(Step.created_at.desc()).limit(1)
            )).scalar_one_or_none()
        if not step:
            res.error = "No executed result available to export"
            return res, None, None

        df, _ = await StepService().export_step_to_csv(db, step.id)
        if df is None or df.empty:
            res.error = "Result is empty — nothing to export"
            return res, None, None

        fmt = spec.format if spec.format in ("csv", "xlsx") else _DEFAULT_FORMAT[spec.ref_type]
        base = spec.filename or _slugify(title or step.title or "export")
        base = re.sub(r"\.(csv|xlsx)$", "", base, flags=re.IGNORECASE)

        if fmt == "xlsx":
            buf = BytesIO()
            try:
                df.to_excel(buf, index=False)
            except Exception as e:
                res.error = f"XLSX export unavailable ({e}); try format='csv'"
                return res, None, None
            content = buf.getvalue()
        else:
            fmt = "csv"
            content = df.to_csv(index=False).encode("utf-8")

        filename = f"{base}.{fmt}"
        temp_path = _write_temp(content, suffix=f".{fmt}")
        maintype, subtype = _MIME[fmt]
        res.filename = filename
        res.success = True
        return res, _att_dict(temp_path, filename, maintype, subtype), temp_path

    async def _resolve_artifact(self, spec, db, report, res):
        """artifact_id -> PPTX (slides) or PDF (page)."""
        from app.models.artifact import Artifact

        report_id, _ = self._scope_ids(report, None)

        artifact = await db.get(Artifact, spec.ref_id)
        if not artifact or artifact.deleted_at is not None:
            res.error = "Artifact not found"
            return res, None, None
        if report_id and str(artifact.report_id) != report_id:
            res.error = "Artifact does not belong to this report"
            return res, None, None

        mode = (artifact.mode or "page").lower()
        default_fmt = "pptx" if mode == "slides" else "pdf"
        fmt = spec.format if spec.format in ("pptx", "pdf") else default_fmt
        base = spec.filename or _slugify(artifact.title or "artifact")
        base = re.sub(r"\.(pptx|pdf)$", "", base, flags=re.IGNORECASE)

        if fmt == "pptx":
            if mode != "slides":
                res.error = "PPTX export is only available for slides artifacts"
                return res, None, None
            slides = (artifact.content or {}).get("slides") or []
            if not slides:
                res.error = "Artifact has no slides to export"
                return res, None, None
            from app.services.pptx_export_service import PptxExportService
            buf = PptxExportService().generate_pptx(slides, title=artifact.title or "Presentation")
            content = buf.getvalue()
            filename = f"{base}.pptx"
            temp_path = _write_temp(content, suffix=".pptx")
            maintype, subtype = _MIME["pptx"]
            res.filename = filename
            res.success = True
            return res, _att_dict(temp_path, filename, maintype, subtype), temp_path

        # pdf
        from app.services.report_pdf_service import ReportPdfService
        pdf_path = await ReportPdfService().generate_for_artifact(str(artifact.id))
        if not pdf_path or not os.path.isfile(pdf_path):
            res.error = "PDF generation failed"
            return res, None, None
        filename = f"{base}.pdf"
        maintype, subtype = _MIME["pdf"]
        res.filename = filename
        res.success = True
        # pdf_path is a persistent file under uploads/ — do NOT schedule it for cleanup.
        return res, _att_dict(pdf_path, filename, maintype, subtype), None

    async def _resolve_file(self, spec, db, organization, res):
        """file_id -> attach the uploaded file as-is."""
        from app.models.file import File

        _, org_id = self._scope_ids(None, organization)

        f = await db.get(File, spec.ref_id)
        if not f:
            res.error = "File not found"
            return res, None, None
        if org_id and str(f.organization_id) != org_id:
            res.error = "File does not belong to this organization"
            return res, None, None
        if not f.path or not os.path.isfile(f.path):
            res.error = "File is no longer available on disk"
            return res, None, None

        filename = spec.filename or f.filename or os.path.basename(f.path)
        ctype = f.content_type or "application/octet-stream"
        if "/" in ctype:
            maintype, subtype = ctype.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        res.filename = filename
        res.success = True
        # f.path is the persistent upload — do NOT schedule it for cleanup.
        return res, _att_dict(f.path, filename, maintype, subtype), None
