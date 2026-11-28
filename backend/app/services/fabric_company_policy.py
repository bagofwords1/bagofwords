"""Resolve the warehouse employee from an organization's verified Entra membership."""
import json
import time
from uuid import UUID
from datetime import datetime, timedelta
from typing import Literal

from pydantic import model_validator

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select, update

from app.data_sources.fabric_company_rls import CompanyPolicy
from app.models.membership import Membership
from app.models.oauth_account import OAuthAccount
from app.models.user import User


POLICY_CONNECTORS = {"ms_fabric", "postgresql", "mssql", "oracledb"}


class ConnectionCompanyPolicy(CompanyPolicy):
    inventory: tuple[str, ...] = ()
    tenant_id: UUID | None = None
    reader_source: Literal["entra", "verified_email"] = "entra"

    @model_validator(mode="after")
    def validate_tenant(self):
        if self.reader_source == "entra" and not self.tenant_id:
            raise ValueError("An Entra policy requires its tenant")
        return self


async def employee_for(db, organization_id, user, policy: ConnectionCompanyPolicy) -> str:
    if user is None:
        raise ValueError("A verified employee identity is required")
    if policy.reader_source == "verified_email":
        login = await db.scalar(select(User.email).join(Membership, Membership.user_id == User.id).where(
            User.id == str(user.id), User.is_verified.is_(True), User.is_active.is_(True),
            Membership.organization_id == str(organization_id), Membership.deleted_at.is_(None)))
        if not login:
            raise ValueError("A verified workspace member is required")
        return str(TypeAdapter(EmailStr).validate_python(login)).lower()
    stmt = (
        select(OAuthAccount.entra_identity)
        .join(User, User.id == OAuthAccount.user_id)
        .join(Membership, Membership.user_id == User.id)
        .where(User.id == str(user.id), User.is_active.is_(True),
               Membership.organization_id == str(organization_id), Membership.deleted_at.is_(None),
               OAuthAccount.entra_identity["tenant_id"].as_string() == str(policy.tenant_id))
    )
    claims = (await db.scalars(stmt)).all()
    if len(claims) == 1 and (claims[0].get("employee_login_exp") or 0) > time.time():
        try:
            return str(TypeAdapter(EmailStr).validate_python(claims[0].get("employee_login"))).lower()
        except ValidationError:
            pass
    raise ValueError("A verified employee identity is required")


async def policy_identity(db, organization_id, user, policy):
    from app.services.rls_identity_service import resolve_identity
    employee = await employee_for(db, organization_id, user, policy)
    identity = await resolve_identity(db, user, organization_id)
    if identity.is_anonymous:
        raise ValueError("An active workspace member is required")
    identity.email = employee
    verified = await db.scalar(select(Membership.profile_verified_at).where(
        Membership.user_id == str(user.id), Membership.organization_id == str(organization_id),
        Membership.deleted_at.is_(None)))
    if not verified or verified < datetime.utcnow() - timedelta(days=1):
        identity.profile_attributes = {}
    return employee, identity


async def bind_company_reader(db, connection, client, viewer):
    """Called after credentials are resolved. A credential owner is never a default viewer."""
    policy_data = getattr(connection, "active_company_policy", None)
    if policy_data is None:
        return client
    from app.data_sources.clients.ms_fabric_client import MsFabricClient
    from app.data_sources.clients._protected_sql import ProtectedSql
    if not isinstance(client, (MsFabricClient, ProtectedSql)):
        raise ValueError("Company-filtered connection unavailable")
    config = json.loads(connection.config) if isinstance(connection.config, str) else connection.config
    keys = (("server_hostname", "database") if connection.type == "ms_fabric" else
            ("host", "port", "service_name" if connection.type.lower() == "oracledb" else "database"))
    if connection.auth_policy != "system_only" or any(
        str(getattr(client, k)) != str(config.get(k)) for k in keys):
        raise ValueError("Protected connection destination or authentication changed")
    policy = ConnectionCompanyPolicy.model_validate(policy_data)
    employee, identity = await policy_identity(db, connection.organization_id, viewer, policy)
    client.bind_company_reader(policy, employee, identity)
    return client

async def reader_columns(db, connection, user):
    policy = ConnectionCompanyPolicy.model_validate(connection.active_company_policy)
    if not any(r.columns is not None for r in policy.tables):
        return {}
    try:
        employee, identity = await policy_identity(db, connection.organization_id, user, policy)
    except ValueError:
        return {r.table: set() for r in policy.tables}
    return {r.table: set(r.visible_columns(employee, identity)) for r in policy.tables if r.columns is not None}


def visible_company_tables(connection):
    policy_data = getattr(connection, "active_company_policy", None)
    if policy_data is None:
        return None
    return {rule.table for rule in ConnectionCompanyPolicy.model_validate(policy_data).tables}


async def activate_company_policy(db, organization, connection_id, expected, actor):
    """Validate and commit the draft and the workspace hold in one transaction."""
    import asyncio

    from fastapi import HTTPException
    from app.ee.audit.service import audit_service
    from app.models.connection import Connection
    from app.services.connection_service import ConnectionService

    connection = await db.scalar(select(Connection).where(
        Connection.id == connection_id, Connection.organization_id == organization.id,
        Connection.deleted_at.is_(None),
    ).with_for_update().execution_options(populate_existing=True))
    if connection is None or connection.type.lower() not in POLICY_CONNECTORS:
        raise HTTPException(404, "Fabric connection unavailable")
    payload = expected.model_dump(mode="json")
    if not connection.company_policy or expected != ConnectionCompanyPolicy.model_validate(connection.company_policy):
        raise HTTPException(409, "Reload the saved mapping before activation")
    if connection.auth_policy != "system_only":
        raise HTTPException(409, "A system-credential Fabric connection is required")
    try:
        await employee_for(db, organization.id, actor, expected)
        client = await ConnectionService().construct_client(db, connection, actor)
        await asyncio.to_thread(client.validate_company_policy, expected)
    except ValueError:
        raise HTTPException(400, "Company policy validation failed") from None

    activated = await db.execute(update(Connection).where(
        Connection.id == connection_id, Connection.updated_at == connection.updated_at,
        Connection.deleted_at.is_(None),
    ).values(active_company_policy=payload))
    if activated.rowcount != 1:
        raise HTTPException(409, "The connection changed during validation")
    organization.withhold_unverified_company_content = True
    await audit_service.log(db, organization_id=str(organization.id), user_id=str(actor.id),
                            action="connection.company_policy.activated", resource_type="connection",
                            resource_id=str(connection.id), details={"table_count": len(expected.tables)}, commit=False)
    await db.commit()
