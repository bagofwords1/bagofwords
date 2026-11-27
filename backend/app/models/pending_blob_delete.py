"""File bytes a delete could not remove yet (retention, or any file delete).

Deleting a file removes its row first and its bytes last
(``file_service.discard_file_bytes``). When removing the bytes fails — the
object store is down, a disk is read only — the path is kept here and the
nightly retention job tries again, so deleted content does not linger in
storage once nothing points at it.
"""
from sqlalchemy import Column, DateTime, Integer, String, Text

from app.models.base import BaseSchema


class PendingBlobDelete(BaseSchema):
    __tablename__ = "pending_blob_deletes"

    path = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False, default=1, server_default="1")
    last_error = Column(String(500), nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
