"""Put corpus dashboards into the state phase 4 serves (phase 4).

Two states these helpers write directly:

- An existing dashboard's owner and project. New dashboards get them when
  they are created; dashboards that predate the column get them from the
  data migration (phase 9).
- A published release, at once: the copy engine (phase 3) as Publish runs it,
  without Publish's run of every copy (phase 5, test_dashboard_publish.py),
  so the copies keep the corpus's rows.

Sharing goes through the API, so it is not here.
"""
import asyncio
from datetime import datetime

from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import ArtifactRelease
from app.services.dashboard_copy import copy_dashboard


def own(rows, corpus, artifact_key, *, owner="owner", project=None):
    """Give a corpus dashboard its owner (and project), as phase 9 will."""
    aid = corpus.ids[artifact_key]
    rows.update("artifacts", aid, owner_id=corpus.cast.id(owner),
                project_id=corpus.cast.projects[project] if project else None)
    return aid


async def _publish(artifact_id: str, *, status: str) -> str:
    async with async_session_maker() as db:
        artifact = await db.get(Artifact, artifact_id)
        version = (await db.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == artifact_id,
                ArtifactVersion.deleted_at.is_(None),
                ArtifactVersion.status == "completed",
            ).order_by(ArtifactVersion.version.desc()).limit(1)
        )).scalar_one()
        release = ArtifactRelease(artifact_id=artifact_id, organization_id=artifact.organization_id,
                                  status=status, published_by=artifact.owner_id or artifact.created_by,
                                  published_at=datetime(2026, 4, 1))
        db.add(release)
        await db.flush()
        await copy_dashboard(db, version, release)
        artifact.published_release_id = release.id
        await db.commit()
        return str(release.id)


# Releases published in the running test: their copied files (thumbnail,
# deck, slide images) are removed after it (conftest), and only theirs --
# other tests copy files in parallel.
PUBLISHED: list[str] = []


def publish(artifact_id: str, *, status: str = "ready") -> str:
    """Publish the dashboard's newest completed version; returns the release id."""
    release_id = asyncio.run(_publish(artifact_id, status=status))
    PUBLISHED.append(release_id)
    return release_id


def uploads_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[3] / "uploads"


def remove_release_files(release_ids) -> None:
    import shutil

    uploads = uploads_dir()
    for rid in release_ids:
        for path in (uploads / "thumbnails" / f"release-{rid}.png", uploads / "pptx" / f"release-{rid}.pptx"):
            path.unlink(missing_ok=True)
        shutil.rmtree(uploads / "pptx_previews" / f"release-{rid}", ignore_errors=True)


def as_(corpus, person):
    """Headers for a corpus person, or none for 'anonymous'."""
    if person == "anonymous":
        return {}
    return corpus.cast.people[person].headers(corpus.cast.org_id)


def share(test_client, corpus, artifact_id, visibility, *, users=None, groups=None, by="owner"):
    body = {"visibility": visibility}
    if users is not None:
        body["user_ids"] = [corpus.cast.id(u) for u in users]
    if groups is not None:
        body["group_ids"] = [corpus.cast.groups[g] for g in groups]
    return test_client.put(f"/api/artifacts/{artifact_id}/sharing", json=body, headers=as_(corpus, by))
