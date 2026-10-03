"""Seed artifacts through the production factory.

Tests used to build rows with `ArtifactVersion(... version=N)` directly, each
one hardcoding its own idea of the numbering rule. That made every change to
how versions are minted a 14-file edit, and let a fixture drift into a state
the application can no longer produce. Route the seeding through the same
`new_artifact` / `new_version` the tools use instead.

These are plain coroutines, not pytest fixtures: most artifact tests already
open their own `async_session_maker()` session and seed inside it.
"""

from typing import Any, Optional

from app.models.artifact import ArtifactVersion
from app.services.artifact_service import new_artifact, new_version


async def seed_artifact(
    db,
    *,
    report_id: str,
    user_id: str,
    organization_id: str,
    mode: str = "page",
    title: str = "Dashboard",
    content: Optional[dict] = None,
    contents: Optional[list[dict]] = None,
    **version_fields: Any,
) -> ArtifactVersion:
    """Seed one artifact and return its LAST version.

    Pass `content` for a single-version artifact, or `contents` for a whole
    chain (v1, v2, ... in order) under one artifact. Extra keyword arguments
    (status, thumbnail_path, created_at, ...) go to every version row.
    """
    if content is not None and contents is not None:
        raise TypeError("pass either content or contents, not both")
    bodies = contents if contents is not None else [content if content is not None else _default_content(mode)]
    if not bodies:
        raise TypeError("contents must not be empty")

    version = await new_artifact(
        db,
        report_id=report_id,
        user_id=user_id,
        organization_id=organization_id,
        mode=mode,
        title=title,
        content=bodies[0],
        **version_fields,
    )
    for body in bodies[1:]:
        version = await new_version(db, version, content=body, **version_fields)
    return version


def _default_content(mode: str) -> dict:
    """The smallest content a renderer of this mode accepts."""
    if mode == "doc":
        return {"markdown": "# Doc", "visualization_ids": []}
    return {"code": "function App() {}", "visualization_ids": []}
