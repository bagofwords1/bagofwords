"""Full-text index of a file connection (index tier ``fulltext``).

Two tables:

- ``file_index_files`` — one row per indexed file: its version (size+mtime or
  etag), status and section count. This is the index's own bookkeeping, written
  per file as a run goes, so an interrupted run keeps what it finished and the
  next run skips files whose version is unchanged.
- ``file_index_sections`` — one row per section of a file (a PDF page, a
  sheet, a few thousand characters of text) with its location in ``read_file``
  coordinates. The searchable terms live next to it, in a dialect-specific
  structure created by the migration and written with raw SQL
  (``app/services/file_index_store.py``):
    * Postgres — a ``tsv tsvector`` column on this table, GIN-indexed;
    * SQLite   — the FTS5 table ``file_index_fts``, keyed by this row's id.

The index holds the words of each section and their positions — not the files.
It is customer content all the same and is removed with the connection.
"""
from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.models.base import Base

# ``file_index_files.status``
FILE_INDEXED = "indexed"      # all extractable text is in the index
FILE_PARTIAL = "partial"      # indexed up to a size cap; the rest is not searchable
FILE_NO_TEXT = "no_text"      # nothing extractable (scanned PDF, image, binary)
FILE_ERROR = "error"          # extraction failed; findable by name only


class FileIndexFile(Base):
    __tablename__ = "file_index_files"
    __table_args__ = (
        UniqueConstraint("connection_id", "file_key", name="uq_file_index_files_conn_key"),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    connection_id = Column(
        String(36), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # sha1 of file_id: file ids are paths of any length, the key is what the
    # unique index is built on.
    file_key = Column(String(40), nullable=False)
    file_id = Column(Text, nullable=False)
    path = Column(Text, nullable=True)
    # What the source reported when the file was indexed (size:mtime, etag…).
    # Equal version → the stored sections are current and the file is skipped.
    version = Column(String(255), nullable=True)
    status = Column(String(16), nullable=False, default=FILE_INDEXED)
    section_count = Column(Integer, nullable=False, default=0)
    char_count = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, default=0)
    index_version = Column(Integer, nullable=False, default=1)
    # JSON object as text: what the catalog row needs back when this file is
    # skipped on a later run ({"keywords": [...], "content_hash": "..."}), so a
    # resumed run does not re-read a file just to rebuild its catalog row.
    meta = Column(Text, nullable=True)
    indexed_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class FileIndexSection(Base):
    __tablename__ = "file_index_sections"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    connection_id = Column(String(36), nullable=False, index=True)
    file_ref = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("file_index_files.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    section_no = Column(Integer, nullable=False, default=0)
    # name | page | sheet | chars | bytes — see _fulltext.Section
    kind = Column(String(16), nullable=False)
    # JSON object as text: {"page": 7} / {"sheet": "Q3"} / {"offset": 0, "length": 3000}
    loc = Column(Text, nullable=True)
    token_count = Column(Integer, nullable=False, default=0)
