from sqlalchemy import Boolean, Column, JSON, String, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class Query(BaseSchema):
    __tablename__ = 'queries'

    # Minimal v0: treat Query as a facade for Widget ownership within a Report
    title = Column(String, nullable=False, default="")
    description = Column(String, nullable=True, default=None)

    # --- access policy (row- and column-level security) ---------------------
    # Authored by the REPORT CREATOR against this query, enforced when someone
    # ELSE views the report or its artifact. Distinct from the connection-level
    # policy on ConnectionTable (admin-authored, on materialized relations):
    # same policy vocabulary and the same compiler, different owner and a
    # different enforcement point — here every `execute_query` return value is
    # filtered in app/services/query_access_policy.py.
    #
    # The policy lives on Query rather than Step because Step rows are
    # regenerated on every rerun while the Query is the stable identity of
    # "this tracked insight" across versions.
    rls_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    rls_mode = Column(String, nullable=True)          # 'attribute' | 'sql' (phase 2)
    rls_policy = Column(JSON, nullable=True)
    # Unresolved identity → no rows. Starts closed, for the same reason it does
    # on ConnectionTable: the failure mode of the opposite default is handing
    # over the whole result.
    rls_default_deny = Column(Boolean, nullable=False, default=True, server_default="1")

    # Column-level security. An allowlist of RESTRICTIONS: a column named here
    # is masked for identities with no matching grant, and every column NOT
    # named stays visible to everyone.
    cls_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    cls_policy = Column(JSON, nullable=True)

    # Owning report (optional). Allows global/public queries not tied to a report.
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)
    report = relationship("Report", back_populates="queries", lazy="selectin")

    

    # Transitional: 1:1 linkage with a Widget so we don't orphan Steps
    widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=False, index=True)
    widget = relationship("Widget", lazy="selectin")

    # Optional: default step for this query (version follow)
    default_step_id = Column(String(36), ForeignKey('steps.id'), nullable=True, index=True)
    # Disambiguate the Step<->Query relationships due to two FKs between these tables
    steps = relationship("Step", back_populates="query", lazy="selectin", foreign_keys="Step.query_id")
    default_step = relationship("Step", foreign_keys=[default_step_id], lazy="selectin")

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True, index=True)
    organization = relationship("Organization", back_populates="queries", lazy="joined")  # to-one: fold into parent query

    user_id = Column(String(36), ForeignKey('users.id'), nullable=True, index=True)
    user = relationship("User", back_populates="queries", lazy="joined")  # to-one: fold into parent query

    # Visualizations owned by this query
    visualizations = relationship("Visualization", back_populates="query", lazy="selectin")

