from sqlalchemy import Column, ForeignKey, Index, Integer, String

from app.ee.encryption import EncryptedJSON
from app.models.base import BaseSchema


class AppRecord(BaseSchema):
    """One record of an artifact app's data (design spec section 5).

    A fixed envelope the server uses to find and authorize records
    (organization, report, artifact, collection, author, version) plus the
    free-form `data` the app defines. Rows hang off the stable `Artifact.id`,
    not a version, so data survives every edit and rebuild. The server filters
    by envelope columns only, never inside `data`, so behavior is identical on
    SQLite and Postgres.
    """
    __tablename__ = 'app_records'
    __table_args__ = (
        Index('ix_app_records_artifact_collection', 'artifact_id', 'collection'),
        Index('ix_app_records_artifact_collection_user', 'artifact_id', 'collection', 'user_id'),
    )

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    # Denormalized for per-report cleanup
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    artifact_id = Column(String(36), ForeignKey('artifacts.id'), nullable=False)
    collection = Column(String(64), nullable=False)
    # Author / owner of the record
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    # Optimistic concurrency counter, starts at 1
    version = Column(Integer, nullable=False, default=1, server_default='1')
    data = Column(EncryptedJSON, nullable=False, default=dict)
