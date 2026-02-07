import re
from typing import List, Dict, Any
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from lxml import html as lxml_html

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user as current_user_dep
from app.core.permissions_decorator import requires_permission

from app.models.user import User
from app.models.organization import Organization
from app.models.artifact import Artifact as ArtifactModel
from app.schemas.artifact_schema import (
    ArtifactSchema,
    ArtifactListSchema,
    ArtifactCreate,
    ArtifactUpdate,
    ArtifactRecentSchema,
)
from app.services.artifact_service import ArtifactService
from app.services.pptx_export_service import PptxExportService
from app.services.thumbnail_service import ThumbnailService


router = APIRouter(prefix="/artifacts", tags=["artifacts"])
service = ArtifactService()
thumbnail_service = ThumbnailService()


@router.get("/recent", response_model=List[ArtifactRecentSchema])
@requires_permission("view_reports")
async def list_recent_artifacts(
    limit: int = Query(6, ge=1, le=20),
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """List recent artifacts for the home page with thumbnails."""
    artifacts = await service.list_recent(db, organization_id=organization.id, limit=limit)
    return [ArtifactRecentSchema.from_artifact(a) for a in artifacts]


@router.get("/{artifact_id}/thumbnail")
@requires_permission("view_reports", model=ArtifactModel, owner_only=True, allow_public=True)
async def get_artifact_thumbnail(
    artifact_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Get thumbnail image for an artifact."""
    artifact = await service.get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Check if thumbnail exists on filesystem
    thumbnail_path = thumbnail_service.get_thumbnail_path(artifact_id)
    if thumbnail_path and thumbnail_path.exists():
        return FileResponse(
            str(thumbnail_path),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    raise HTTPException(status_code=404, detail="Thumbnail not available")


def _get_text_content(element) -> str:
    """Extract text content from an lxml element, stripping tags."""
    if element is None:
        return ""
    return " ".join(element.text_content().split()).strip()


def _has_class(element, class_name: str) -> bool:
    """Check if element has a specific class."""
    classes = element.get("class", "")
    return class_name in classes.split()


def _parse_slides_from_html(html_code: str) -> List[Dict[str, Any]]:
    """Parse HTML slides code to extract slide structure for PPTX export.

    Uses structured pptx-* CSS classes for reliable extraction.
    Falls back to heuristic parsing for older slides.
    """
    slides = []

    try:
        doc = lxml_html.fromstring(html_code)
    except Exception:
        return []

    # Find all slide sections
    slide_elements = doc.xpath('//section[contains(@class, "slide")]')

    for slide_el in slide_elements:
        slide_num = slide_el.get("data-slide", "0")
        slide_type = slide_el.get("data-type", "")  # New: explicit type from data attribute
        slide_data: Dict[str, Any] = {"type": slide_type or "text"}

        # === PPTX-CLASS BASED EXTRACTION (preferred) ===

        # Title (pptx-title class)
        title_el = slide_el.xpath('.//*[contains(@class, "pptx-title")]')
        if title_el:
            slide_data["title"] = _get_text_content(title_el[0])
            if not slide_type:
                slide_data["type"] = "title"

        # Heading (pptx-heading class)
        heading_el = slide_el.xpath('.//*[contains(@class, "pptx-heading")]')
        if heading_el and "title" not in slide_data:
            slide_data["title"] = _get_text_content(heading_el[0])

        # Subtitle (pptx-subtitle class)
        subtitle_el = slide_el.xpath('.//*[contains(@class, "pptx-subtitle")]')
        if subtitle_el:
            slide_data["subtitle"] = _get_text_content(subtitle_el[0])

        # Metrics (pptx-metric class)
        metric_els = slide_el.xpath('.//*[contains(@class, "pptx-metric")]')
        if metric_els:
            if not slide_type:
                slide_data["type"] = "metrics"
            metrics = []
            for metric_el in metric_els[:4]:
                value_el = metric_el.xpath('.//*[contains(@class, "pptx-metric-value")]')
                label_el = metric_el.xpath('.//*[contains(@class, "pptx-metric-label")]')
                change_el = metric_el.xpath('.//*[contains(@class, "pptx-metric-change")]')
                metrics.append({
                    "value": _get_text_content(value_el[0]) if value_el else "",
                    "label": _get_text_content(label_el[0]) if label_el else "",
                    "change": _get_text_content(change_el[0]) if change_el else None,
                })
            slide_data["metrics"] = metrics

        # Bullets (pptx-bullet class)
        bullet_els = slide_el.xpath('.//*[contains(@class, "pptx-bullet")]')
        if bullet_els:
            if not slide_type and slide_data["type"] == "text":
                slide_data["type"] = "bullets"
            bullets = [_get_text_content(b) for b in bullet_els[:8]]
            bullets = [b for b in bullets if b and len(b) > 2]
            if bullets:
                slide_data["bullets"] = bullets

        # Insight (pptx-insight class)
        insight_el = slide_el.xpath('.//*[contains(@class, "pptx-insight")]')
        if insight_el:
            slide_data["insight"] = _get_text_content(insight_el[0])

        # Code (pptx-code class)
        code_els = slide_el.xpath('.//*[contains(@class, "pptx-code")]')
        if code_els:
            if not slide_type and slide_data["type"] == "text":
                slide_data["type"] = "code"
            code_snippets = [_get_text_content(c) for c in code_els[:3]]
            slide_data["code_snippets"] = code_snippets

        # Chart placeholder (pptx-chart class)
        chart_el = slide_el.xpath('.//*[contains(@class, "pptx-chart")]')
        if chart_el:
            if not slide_type and slide_data["type"] == "text":
                slide_data["type"] = "chart"
            slide_data["chartType"] = chart_el[0].get("data-chart-type", "bar")
            slide_data["vizId"] = chart_el[0].get("data-viz-id", None)

        # === FALLBACK: HEURISTIC EXTRACTION (for older slides without pptx-* classes) ===

        if "title" not in slide_data:
            # Try h1 or h2
            h1_el = slide_el.xpath('.//h1')
            h2_el = slide_el.xpath('.//h2')
            if h1_el:
                slide_data["title"] = _get_text_content(h1_el[0])
                if slide_num == "0" and not slide_type:
                    slide_data["type"] = "title"
            elif h2_el:
                slide_data["title"] = _get_text_content(h2_el[0])

        if "subtitle" not in slide_data and slide_data.get("type") == "title":
            # Look for subtitle in p tags with slate/muted styling
            p_els = slide_el.xpath('.//p[contains(@class, "text-slate") or contains(@class, "text-2xl") or contains(@class, "text-xl")]')
            if p_els:
                subtitle = _get_text_content(p_els[0])
                if subtitle and len(subtitle) > 5:
                    slide_data["subtitle"] = subtitle

        # Fallback: metrics from large text elements
        if "metrics" not in slide_data:
            value_els = slide_el.xpath('.//*[contains(@class, "text-5xl") or contains(@class, "text-6xl") or contains(@class, "text-4xl")]')
            if len(value_els) >= 2:
                if not slide_type:
                    slide_data["type"] = "metrics"
                metrics = []
                for val_el in value_els[:4]:
                    value = _get_text_content(val_el)
                    # Try to find sibling label
                    parent = val_el.getparent()
                    label_el = parent.xpath('.//*[contains(@class, "text-slate") or contains(@class, "text-sm")]') if parent is not None else []
                    label = _get_text_content(label_el[0]) if label_el else ""
                    metrics.append({"value": value, "label": label})
                slide_data["metrics"] = metrics

        # Fallback: bullets from li elements
        if "bullets" not in slide_data:
            li_els = slide_el.xpath('.//li')
            if li_els and slide_data["type"] == "text":
                slide_data["type"] = "bullets"
                bullets = [_get_text_content(li) for li in li_els[:8]]
                bullets = [b for b in bullets if b and len(b) > 2]
                if bullets:
                    slide_data["bullets"] = bullets

        # Fallback: code from pre elements
        if "code_snippets" not in slide_data:
            pre_els = slide_el.xpath('.//pre')
            if pre_els:
                if slide_data["type"] == "text":
                    slide_data["type"] = "code"
                code_snippets = [_get_text_content(p) for p in pre_els[:3]]
                slide_data["code_snippets"] = code_snippets

        # Fallback: text content from paragraphs
        if slide_data["type"] == "text" and "text" not in slide_data:
            p_els = slide_el.xpath('.//p')
            if p_els:
                paragraphs = [_get_text_content(p) for p in p_els[:5]]
                paragraphs = [p for p in paragraphs if p and len(p) > 10]
                if paragraphs:
                    slide_data["text"] = '\n\n'.join(paragraphs)[:800]

        slides.append(slide_data)

    return slides


@router.post("", response_model=ArtifactSchema)
@requires_permission('update_reports')
async def create_artifact(
    payload: ArtifactCreate,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Create a new artifact for a report."""
    artifact = await service.create(
        db,
        payload,
        user_id=current_user.id,
        organization_id=organization.id,
    )
    return ArtifactSchema.model_validate(artifact)


@router.get("/{artifact_id}", response_model=ArtifactSchema)
@requires_permission('view_reports', model=ArtifactModel, owner_only=True, allow_public=True)
async def get_artifact(
    artifact_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Get an artifact by ID."""
    artifact = await service.get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return ArtifactSchema.model_validate(artifact)


@router.get("/report/{report_id}", response_model=List[ArtifactListSchema])
@requires_permission('view_reports')
async def list_artifacts_by_report(
    report_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """List all artifacts for a report."""
    artifacts = await service.list_by_report(db, report_id)
    return [ArtifactListSchema.model_validate(a) for a in artifacts]


@router.get("/report/{report_id}/latest", response_model=ArtifactSchema)
@requires_permission('view_reports')
async def get_latest_artifact(
    report_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Get the latest artifact for a report."""
    artifact = await service.get_latest_by_report(db, report_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="No artifacts found for this report")
    return ArtifactSchema.model_validate(artifact)


@router.patch("/{artifact_id}", response_model=ArtifactSchema)
@requires_permission('update_reports', model=ArtifactModel, owner_only=True)
async def update_artifact(
    artifact_id: str,
    payload: ArtifactUpdate,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Update an existing artifact."""
    artifact = await service.update(db, artifact_id, payload)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return ArtifactSchema.model_validate(artifact)


@router.delete("/{artifact_id}")
@requires_permission('update_reports', model=ArtifactModel, owner_only=True)
async def delete_artifact(
    artifact_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Delete an artifact (soft delete)."""
    success = await service.delete(db, artifact_id)
    if not success:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return {"status": "deleted"}


@router.post("/{artifact_id}/duplicate", response_model=ArtifactSchema)
@requires_permission('update_reports', model=ArtifactModel, owner_only=True)
async def duplicate_artifact(
    artifact_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Duplicate an artifact to make it the latest (default) version."""
    artifact = await service.duplicate(db, artifact_id, user_id=current_user.id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return ArtifactSchema.model_validate(artifact)


@router.get("/{artifact_id}/export/pptx")
@requires_permission('view_reports', model=ArtifactModel, owner_only=True, allow_public=True)
async def export_artifact_pptx(
    artifact_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Export a slides artifact as PowerPoint (PPTX)."""
    artifact = await service.get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if artifact.mode != "slides":
        raise HTTPException(status_code=400, detail="Only slides artifacts can be exported to PPTX")

    # Get slides data from artifact content (or parse from HTML as fallback)
    slides_data = artifact.content.get("slides") if artifact.content else None
    if not slides_data:
        # Fallback: parse slides structure from HTML code
        html_code = artifact.content.get("code", "") if artifact.content else ""
        if html_code:
            slides_data = _parse_slides_from_html(html_code)

    if not slides_data:
        raise HTTPException(status_code=400, detail="No slides data available for export")

    # Generate PPTX
    pptx_service = PptxExportService()
    pptx_buffer = pptx_service.generate_pptx(
        slides=slides_data,
        title=artifact.title or "Presentation"
    )

    # Return as downloadable file
    # Sanitize filename for HTTP headers (ASCII only)
    safe_title = (artifact.title or "presentation").encode("ascii", "ignore").decode("ascii")
    safe_title = re.sub(r'[^\w\s-]', '', safe_title).strip() or "presentation"
    filename = f"{safe_title}.pptx"
    return StreamingResponse(
        pptx_buffer,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
