from sqlalchemy import Column, String, ForeignKey
from app.models.base import BaseSchema


class ArtifactSlugHistory(BaseSchema):
    """
    A slug that is no longer an artifact's current one.

    - artifact_id set: an earlier name of that artifact, kept so links already
      sent keep working — /r/{old slug} resolves to it and the page moves to
      its current slug.
    - artifact_id NULL: a released name (cleared by the owner, or its
      artifact/report gone). It resolves nowhere, and only the organization
      that held it may claim it again — a link already sent can never be
      taken over by another organization.

    `slug` is unique here and never equal to any artifacts.slug — together
    they form one namespace.
    """
    __tablename__ = 'artifact_slug_history'

    slug = Column(String(80), nullable=False, unique=True, index=True)
    artifact_id = Column(String(36), ForeignKey('artifacts.id'), nullable=True, index=True)
    # The organization the name belongs to (kept after release).
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
