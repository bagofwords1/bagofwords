from sqlalchemy import Column, String, JSON

from app.models.base import BaseSchema


class InstanceSettings(BaseSchema):
    """Instance-wide settings — deliberately *not* scoped to an organization.

    Almost everything configurable in BOW hangs off ``organization_settings``.
    A handful of things are properties of the deployment rather than of a
    tenant; the enterprise license is the first of them (``get_license_info()``
    takes no organization, and an instance running two organizations still has
    exactly one license). Those live here, one row per ``key``.
    """

    __tablename__ = "instance_settings"

    key = Column(String(64), unique=True, nullable=False, index=True)
    value = Column(JSON, default=dict, nullable=False)
