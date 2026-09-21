from sqlalchemy import Column, String, Text, ForeignKey
from app.models.base import BaseSchema


class AgentCatalog(BaseSchema):
    """A named, org-level grouping of agents (data sources), e.g. "Finance".

    Organizational only: it drives the agents-list filter and carries no
    access semantics — membership in a catalog grants or hides nothing. An
    agent belongs to at most one catalog (data_sources.catalog_id). Managed
    by full admins only.

    Names are unique among live catalogs of an org; that is enforced in the
    service rather than a DB constraint so a soft-deleted catalog doesn't
    block reusing its name.
    """
    __tablename__ = 'agent_catalogs'

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    color = Column(String, nullable=True)
