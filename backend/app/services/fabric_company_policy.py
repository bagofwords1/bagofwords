"""Resolve the warehouse employee from an organization's verified Entra membership."""
import json
import time
from uuid import UUID

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app.data_sources.fabric_company_rls import CompanyPolicy
from app.models.membership import Membership
from app.models.oauth_account import OAuthAccount
from app.models.user import User


class ConnectionCompanyPolicy(CompanyPolicy):
    tenant_id: UUID


async def employee_for(db, organization_id, user, policy: ConnectionCompanyPolicy) -> str:
    if user is None:
        raise ValueError("A verified employee identity is required")
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


async def bind_company_reader(db, connection, client, viewer):
    """Called after credentials are resolved. A credential owner is never a default viewer."""
    policy_data = getattr(connection, "active_company_policy", None)
    if policy_data is None:
        return client
    from app.data_sources.clients.ms_fabric_client import MsFabricClient
    if not isinstance(client, MsFabricClient):
        raise ValueError("Company-filtered connection unavailable")
    config = json.loads(connection.config) if isinstance(connection.config, str) else connection.config
    if (client.database != config.get("database") or client.server_hostname != config.get("server_hostname")
            or connection.auth_policy != "system_only"):
        raise ValueError("Protected connection destination or authentication changed")
    policy = ConnectionCompanyPolicy.model_validate(policy_data)
    employee = await employee_for(db, connection.organization_id, viewer, policy)
    client.bind_company_reader(policy, employee)
    return client

def visible_company_tables(connection):
    policy_data = getattr(connection, "active_company_policy", None)
    if policy_data is None:
        return None
    return {rule.table for rule in ConnectionCompanyPolicy.model_validate(policy_data).tables}
