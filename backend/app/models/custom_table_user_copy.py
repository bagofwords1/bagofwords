from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)

from app.models.base import BaseSchema

# How a user's copy stands.
STATUS_BUILDING = "building"   # an extraction is running (see `building_since`)
STATUS_OK = "ok"               # `artifact_path` holds this user's rows
STATUS_ERROR = "error"         # the last build failed; `error` says why
STATUS_EVICTED = "evicted"     # the file was removed (expired, purged, quota)


class CustomTableUserCopy(BaseSchema):
    """One user's own materialized copy of a custom table (migration ucopy01).

    On a connection where every person queries with their own credentials
    (`auth_policy='user_required'`), one shared artifact would hand every
    reader the rows of whoever built it. So the table's *definition* stays on
    `ConnectionTable`, and the *data* lives here: one encrypted artifact per
    (table, user), extracted with that user's own credentials, so the source's
    own row-level security decides what is in it.

    `definition_hash` and `credential_fingerprint` say what the copy was
    built from. A copy is served only while both still match — an edited
    query, or a user who reconnected as someone else, makes it unusable at
    once rather than at the next refresh.

    The row outlives its file (`STATUS_EVICTED`): it is how a later sign-in
    knows which tables this user actually works with.
    """
    __tablename__ = "custom_table_user_copies"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_custom_table_user_copies"),
        UniqueConstraint("connection_table_id", "user_id",
                         name="uq_custom_table_user_copies_table_user"),
        Index("ix_custom_table_user_copies_user", "user_id"),
        Index("ix_custom_table_user_copies_connection", "connection_id"),
    )

    connection_table_id = Column(
        String(36),
        ForeignKey("connection_tables.id", name="fk_custom_table_user_copies_connection_table_id"),
        nullable=False,
    )
    connection_id = Column(String(36), nullable=False)
    organization_id = Column(
        String(36),
        ForeignKey("organizations.id", name="fk_custom_table_user_copies_organization_id"),
        nullable=False,
    )
    user_id = Column(
        String(36),
        ForeignKey("users.id", name="fk_custom_table_user_copies_user_id"),
        nullable=False,
    )

    status = Column(String(20), nullable=False, default=STATUS_BUILDING,
                    server_default=STATUS_BUILDING)
    error = Column(Text, nullable=True)

    # Same contract as ConnectionTable's artifact columns: an opaque uuid file
    # and its own key, Fernet-encrypted.
    artifact_path = Column(String, nullable=True)
    artifact_key_enc = Column(Text, nullable=True)
    artifact_bytes = Column(BigInteger, nullable=True)
    row_count = Column(BigInteger, nullable=True)
    columns = Column(JSON, nullable=True)

    definition_hash = Column(String(64), nullable=True)
    credential_fingerprint = Column(String(64), nullable=True)
    # What the last FAILED build was attempted with. Kept apart from the pair
    # above, which describes the artifact: a failed rebuild must not relabel
    # the copy still on disk. Used only to avoid repeating the same failure
    # for every question.
    failed_definition_hash = Column(String(64), nullable=True)
    failed_credential_fingerprint = Column(String(64), nullable=True)

    refreshed_at = Column(DateTime, nullable=True)
    build_ms = Column(Integer, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    # Set while a build runs, and renewed by it: a `building` row whose stamp
    # is old belongs to a worker that died, and may be taken over.
    building_since = Column(DateTime, nullable=True)
    # Set by anything that makes the data suspect without changing what it
    # was built from (a sign-in, an admin's "refresh"): the copy is rebuilt
    # before it is served again.
    invalidated_at = Column(DateTime, nullable=True)
    # The softer form: the copy is still served, and rebuilt behind it at its
    # next use (a sign-in asks for this).
    refresh_requested_at = Column(DateTime, nullable=True)
