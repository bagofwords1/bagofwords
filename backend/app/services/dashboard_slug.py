"""A dashboard's name in its link (phase 8).

`/d/{name}` opens the dashboard as `/d/{id}` does; the id link keeps
working with or without a name. The owner sets the name in the dashboard's
settings:

- a name is lowercase English letters, digits and hyphens, 3 to 80 long,
  starting and ending with a letter or digit; never shaped like an id (the
  link would read as one) and not a reserved word;
- unique in the installation (a link carries no organization): neither
  another dashboard's name nor a name another dashboard had;
- renaming keeps the old name leading to the same dashboard
  (artifact_slug_history), and the dashboard may take an old name of its
  own back;
- removing the name stops every name of the dashboard (released_at); the
  names stay taken. A deleted dashboard's names open nothing and stay
  taken, as its row stays.
"""
import re
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactSlugHistory

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RESERVED = frozenset({"api", "admin", "new", "edit", "settings", "login"})


def _invalid() -> AppError:
    return AppError.bad_request(ErrorCode.DASHBOARD_SLUG_INVALID)


def _taken() -> AppError:
    return AppError.conflict(ErrorCode.DASHBOARD_SLUG_TAKEN)


def check_format(slug: str) -> None:
    if not _NAME.match(slug) or _UUID.match(slug) or slug in RESERVED:
        raise _invalid()


async def _taken_by_another(db, artifact: Artifact, slug: str) -> bool:
    current = (await db.execute(
        select(Artifact.id).where(Artifact.slug == slug, Artifact.id != str(artifact.id)).limit(1)
    )).first()
    if current is not None:
        return True
    old = (await db.execute(
        select(ArtifactSlugHistory.artifact_id).where(ArtifactSlugHistory.slug == slug).limit(1)
    )).first()
    return old is not None and str(old[0]) != str(artifact.id)


async def _keep_old_name(db, artifact: Artifact, slug: str, *, released: bool) -> None:
    row = (await db.execute(
        select(ArtifactSlugHistory).where(ArtifactSlugHistory.slug == slug)
    )).scalar_one_or_none()
    if row is None:
        db.add(ArtifactSlugHistory(slug=slug, artifact_id=str(artifact.id),
                                   released_at=datetime.utcnow() if released else None))
    elif released:
        row.released_at = datetime.utcnow()


async def set_slug(db, artifact: Artifact, slug: str) -> None:
    """Name the dashboard `slug`, or remove its name ("" / None). Flushes;
    the caller commits (a race on the same name fails there as taken)."""
    new = (slug or "").strip()
    if new == (artifact.slug or ""):
        return
    if not new:
        if artifact.slug:
            await _keep_old_name(db, artifact, artifact.slug, released=True)
        await db.execute(update(ArtifactSlugHistory).where(
            ArtifactSlugHistory.artifact_id == str(artifact.id), ArtifactSlugHistory.released_at.is_(None),
        ).values(released_at=datetime.utcnow()))
        artifact.slug = None
        await db.flush()
        return
    check_format(new)
    if await _taken_by_another(db, artifact, new):
        raise _taken()
    if artifact.slug:
        await _keep_old_name(db, artifact, artifact.slug, released=False)
    # Taking back an old name of its own: it is the current name again.
    own_old = (await db.execute(
        select(ArtifactSlugHistory).where(ArtifactSlugHistory.slug == new,
                                          ArtifactSlugHistory.artifact_id == str(artifact.id))
    )).scalar_one_or_none()
    if own_old is not None:
        await db.delete(own_old)
        await db.flush()
    artifact.slug = new
    try:
        await db.flush()
    except IntegrityError:
        raise _taken()


async def by_old_name(db, slug: str) -> str | None:
    """The dashboard an old, not removed name leads to."""
    return (await db.execute(
        select(ArtifactSlugHistory.artifact_id).where(
            ArtifactSlugHistory.slug == slug, ArtifactSlugHistory.released_at.is_(None))
    )).scalar_one_or_none()
