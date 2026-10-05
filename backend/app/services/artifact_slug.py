"""Readable share links: /r/{slug} opens one artifact.

A slug is an optional name the owner gives one artifact (dashboard). It is
only a pointer: /r/{slug} resolves to the same report_id + artifact_id that
/r/{report_id}?artifact={id} names, and every access rule stays on that
artifact (artifact_access). The long link keeps working.

Namespace: artifacts.slug (current names) and artifact_slug_history
(earlier names, and released ones) together hold every taken name, across
all organizations — the link carries no org.

- Renaming keeps the previous name pointing at the artifact, so links
  already sent keep working.
- Releasing a name — the owner clears it, or its artifact dies (the artifact
  deleted, or its report soft-deleted) — makes it resolve nowhere and keeps
  it reserved for its organization: a link already sent can never be taken
  over by another organization. A dead artifact's names are released when
  someone next claims one, which covers every way an artifact can die
  without a hook in each. An archived report ("delete" in the UI) is not
  dead — its shared dashboards stay served on the long link, so their names
  keep working too.
"""

import re
import uuid
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import lazyload

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact
from app.models.artifact_slug_history import ArtifactSlugHistory
from app.models.report import Report

MIN_LENGTH = 3
MAX_LENGTH = 80
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED = frozenset({"api", "admin", "new", "edit", "settings", "login"})


def _invalid() -> AppError:
    return AppError(
        ErrorCode.ARTIFACT_SLUG_INVALID,
        f"Use {MIN_LENGTH}-{MAX_LENGTH} lowercase letters, digits and single hyphens",
        status_code=422,
        params={"min": MIN_LENGTH, "max": MAX_LENGTH},
    )


def _looks_like_uuid(value: str) -> bool:
    # /r/{x} tells a report id from a slug by this shape, so a slug may never
    # parse as one (uuid.UUID also accepts the undashed 32-hex form).
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def normalize(raw: str) -> str:
    """The stored form of a requested slug, or AppError(422) if it can't be one."""
    slug = (raw or "").strip().lower()
    if not (MIN_LENGTH <= len(slug) <= MAX_LENGTH):
        raise _invalid()
    if not _SLUG_RE.match(slug) or slug in RESERVED or _looks_like_uuid(slug):
        raise _invalid()
    return slug


def share_path(artifact) -> str:
    """The path that opens this artifact: /r/{slug} when named, else the long form."""
    if getattr(artifact, "slug", None):
        return f"/r/{artifact.slug}"
    return f"/r/{artifact.report_id}?artifact={artifact.id}"


async def _lookup(db, slug: str) -> tuple[Optional[Artifact], Optional[ArtifactSlugHistory]]:
    """The artifact a name points at (if any) and, for a non-current name,
    its history row. A released name returns (None, row)."""
    artifact = (await db.execute(
        select(Artifact).options(lazyload("*")).where(Artifact.slug == slug)
    )).scalar_one_or_none()
    if artifact is not None:
        return artifact, None
    row = (await db.execute(
        select(ArtifactSlugHistory).where(ArtifactSlugHistory.slug == slug)
    )).scalar_one_or_none()
    if row is None or row.artifact_id is None:
        return None, row
    holder = (await db.execute(
        select(Artifact).options(lazyload("*")).where(Artifact.id == str(row.artifact_id))
    )).scalar_one_or_none()
    return holder, row


async def _is_live(db, artifact: Artifact) -> bool:
    if artifact.deleted_at is not None:
        return False
    report_deleted_at = (await db.execute(
        select(Report.deleted_at).where(Report.id == str(artifact.report_id))
    )).first()
    return report_deleted_at is not None and report_deleted_at[0] is None


async def _release_all(db, artifact: Artifact) -> None:
    """Release every name this artifact holds; they stay reserved for its org."""
    await db.execute(
        update(ArtifactSlugHistory)
        .where(ArtifactSlugHistory.artifact_id == str(artifact.id))
        .values(artifact_id=None, organization_id=str(artifact.organization_id))
    )
    if artifact.slug is not None:
        db.add(ArtifactSlugHistory(
            slug=artifact.slug, artifact_id=None, organization_id=str(artifact.organization_id),
        ))
        artifact.slug = None
        db.add(artifact)
    # Flush now: the name may be claimed by another row right after, and the
    # unique indexes must see it released first.
    await db.flush()


def _taken(slug: str) -> AppError:
    return AppError.conflict(ErrorCode.ARTIFACT_SLUG_TAKEN, "This link name is taken", slug=slug)


async def set_slug(db, artifact: Artifact, raw: Optional[str]) -> Optional[str]:
    """Give `artifact` the slug `raw` (None or blank releases every name it has).

    Renaming keeps the previous slug pointing here; re-claiming one of the
    artifact's own earlier slugs moves it back. A released name can be
    claimed again only from the organization that held it.
    Raises AppError 422 (invalid) or 409 (taken).
    """
    if raw is None or not raw.strip():
        await _release_all(db, artifact)
        await db.commit()
        return None

    slug = normalize(raw)
    if slug == artifact.slug:
        return slug

    holder, row = await _lookup(db, slug)
    if holder is not None and str(holder.id) != str(artifact.id):
        if await _is_live(db, holder):
            raise _taken(slug)
        await _release_all(db, holder)
        _, row = await _lookup(db, slug)
    if row is not None and row.artifact_id is None and str(row.organization_id) != str(artifact.organization_id):
        raise _taken(slug)

    await db.execute(delete(ArtifactSlugHistory).where(ArtifactSlugHistory.slug == slug))
    if artifact.slug:
        db.add(ArtifactSlugHistory(
            slug=artifact.slug, artifact_id=str(artifact.id), organization_id=str(artifact.organization_id),
        ))
    artifact.slug = slug
    db.add(artifact)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race for the same name.
        await db.rollback()
        raise _taken(slug)
    return slug


async def resolve(db, raw: str, user) -> dict:
    """{report_id, artifact_id, slug} for a name the caller may open.

    `slug` is the artifact's current name, so a caller that arrived on an
    earlier one can move to it. Unknown, released or dead names are 404; access is the
    artifact's own rule (401 sign-in needed / 403 denied), as on the long link.
    """
    from app.services import artifact_access

    slug = (raw or "").strip().lower()
    holder = (await _lookup(db, slug))[0] if slug else None
    if holder is None or not await _is_live(db, holder):
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND, "Not found")
    report = (await db.execute(
        select(Report).options(lazyload("*")).where(Report.id == str(holder.report_id))
    )).scalar_one()
    await artifact_access.assert_can_view_artifact(db, report, str(holder.id), user)
    return {
        "report_id": str(holder.report_id),
        "artifact_id": str(holder.id),
        "slug": holder.slug,
    }
