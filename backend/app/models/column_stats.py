from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class ColumnStats(BaseSchema):
    """Org-wide usage of one column of one agent table.

    The column-level twin of ``TableStats``: fed from the same once-per-step
    ``TableUsageEvent`` (its ``columns`` list, extracted from the executed
    query) and from thumbs up/down on the completion that used it. Read in one
    batched query by ``SchemaContextBuilder`` to rank which columns of a wide
    table reach the planner's context. Context curation only: it never limits
    what a query may read.
    """

    __tablename__ = "column_stats"

    org_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    data_source_id = Column(String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False)
    # ON DELETE CASCADE: schema re-sync and connection unlink delete
    # DataSourceTable rows with bulk DELETE statements (no ORM cascade).
    datasource_table_id = Column(
        String(36), ForeignKey("datasource_tables.id", ondelete="CASCADE"), nullable=False
    )
    column_name = Column(String(1024), nullable=False)

    # Attempts (success + failure), successes, failures.
    usage_count = Column(BigInteger, nullable=False, default=0)
    success_count = Column(BigInteger, nullable=False, default=0)
    failure_count = Column(BigInteger, nullable=False, default=0)
    # Role-weighted usage. A failed attempt adds a fraction of a success
    # (FAILURE_USAGE_WEIGHT in column_usage_service): a column the agent reached
    # for is still a relevant column, just less proven than one that worked.
    weighted_usage_count = Column(Float, nullable=False, default=0.0)

    pos_feedback_count = Column(BigInteger, nullable=False, default=0)
    neg_feedback_count = Column(BigInteger, nullable=False, default=0)
    weighted_pos_feedback = Column(Float, nullable=False, default=0.0)
    weighted_neg_feedback = Column(Float, nullable=False, default=0.0)

    # weighted_usage_count + weighted_pos_feedback - weighted_neg_feedback,
    # kept current by every upsert. Indexed with the table id so the schema
    # build reads each table's top rows by index order (LIMIT), without
    # sorting the table's whole history on every build.
    rank_weight = Column(Float, nullable=False, default=0.0)

    last_used_at = Column(DateTime, nullable=True)
    last_feedback_at = Column(DateTime, nullable=True)
    updated_at_stats = Column(DateTime, nullable=False, default=datetime.utcnow)

    datasource_table = relationship("DataSourceTable", back_populates="column_stat_rows")

    __table_args__ = (
        # One row per (table, column): the upsert target, and its leading
        # column serves the read path (WHERE datasource_table_id IN (...)).
        UniqueConstraint("datasource_table_id", "column_name", name="uq_colstats_table_column"),
        Index("ix_colstats_ds", "data_source_id"),
        Index("ix_colstats_table_rank", "datasource_table_id", "rank_weight"),
    )
