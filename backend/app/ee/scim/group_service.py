# SCIM 2.0 Group Service
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# Maps SCIM Groups (RFC 7643 §4.2) onto BOW Groups + GroupMemberships.
#
# Ownership: a group created over SCIM is stored with external_provider="scim"
# and is owned by the IdP from then on. The SCIM surface only ever sees and
# touches those groups — hand-made groups and groups synced by LDAP/OIDC are
# invisible to it, and a create whose name collides with one of them is a 409
# rather than a silent takeover. Conversely the admin RBAC API refuses to edit
# a SCIM group (app/services/rbac_service.py), since the next push from the IdP
# would overwrite the edit.
#
# Members are BOW user ids (the `id` the IdP got back from POST /Users) and
# must already be members of the organization: group pushes never create org
# memberships, so user lifecycle and the license seat cap stay with /Users.

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ee.scim.errors import ScimError
from app.ee.scim.schemas import (
    ScimGroup,
    ScimGroupCreate,
    ScimGroupListResponse,
    ScimGroupMember,
    ScimMeta,
    ScimPatchOp,
)
from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.models.membership import Membership
from app.models.report_share import ReportShare
from app.models.resource_grant import ResourceGrant
from app.models.role_assignment import RoleAssignment
from app.models.usage_policy import UsagePolicyAssignment
from app.models.user import User

PROVIDER_NAME = "scim"


# --- Filters ---------------------------------------------------------------
#
# Supported: a conjunction ("and") of equality terms over
#   displayName (case-insensitive, RFC 7643 caseExact=false), externalId, id,
#   members.value / members / members[value eq "..."]
# which covers what Entra ("displayName eq", "id eq ... and members[value eq
# ...]") and Okta ("displayName eq") send. Anything else is a 400
# invalidFilter: answering an unparsed filter with the unfiltered list would
# make the IdP "match" an arbitrary group.

_STRING = r'"((?:[^"\\]|\\.)*)"'
_TERM_RE = re.compile(
    r'^\s*(?:'
    r'(?P<attr>[A-Za-z][\w.]*)\s+eq\s+' + _STRING +
    r'|members\[\s*value\s+eq\s+' + _STRING.replace("(", "(?P<mv>", 1) + r'\s*\]'
    r')\s*$',
    re.IGNORECASE,
)
_AND_SPLIT_RE = re.compile(r'\s+and\s+(?=(?:[^"\\]|\\.|"(?:[^"\\]|\\.)*")*$)', re.IGNORECASE)

_FILTER_ATTRS = {
    "displayname": "displayName",
    "externalid": "externalId",
    "id": "id",
    "members.value": "members.value",
    "members": "members.value",
}


def _unescape(value: str) -> str:
    return re.sub(r'\\(.)', r'\1', value)


def parse_group_filter(filter_str: Optional[str]) -> List[Tuple[str, str]]:
    """Parse a SCIM group filter into [(attribute, value), ...] (all ANDed)."""
    if not filter_str or not filter_str.strip():
        return []
    terms: List[Tuple[str, str]] = []
    for raw in _AND_SPLIT_RE.split(filter_str.strip()):
        match = _TERM_RE.match(raw)
        if not match:
            raise ScimError(400, f"Unsupported filter: {filter_str}", "invalidFilter")
        if match.group("mv") is not None:
            terms.append(("members.value", _unescape(match.group("mv"))))
            continue
        attr = _FILTER_ATTRS.get(match.group("attr").lower())
        if attr is None:
            raise ScimError(400, f"Filtering on '{match.group('attr')}' is not supported", "invalidFilter")
        terms.append((attr, _unescape(match.group(2))))
    return terms


# --- PATCH paths -----------------------------------------------------------

_MEMBER_VALUE_PATH_RE = re.compile(
    r'^members\[\s*value\s+eq\s+' + _STRING + r'\s*\]$', re.IGNORECASE
)


def _member_ids(value) -> List[str]:
    """Member ids from a PATCH/PUT value: [{"value": id}, ...], one dict, or bare ids."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    ids: List[str] = []
    for item in items:
        if isinstance(item, dict):
            member_id = item.get("value")
        elif isinstance(item, ScimGroupMember):
            member_id = item.value
        else:
            member_id = item
        if not isinstance(member_id, str) or not member_id.strip():
            raise ScimError(400, "Every member needs a string 'value'", "invalidValue")
        ids.append(member_id.strip())
    return ids


def _display_name(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScimError(400, "displayName must be a non-empty string", "invalidValue")
    if len(value) > 255:
        raise ScimError(400, "displayName is longer than 255 characters", "invalidValue")
    return value


def _external_id(value) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScimError(400, "externalId must be a string", "invalidValue")
    return value


@dataclass
class _GroupState:
    """Target state of a group, built up while applying PATCH operations."""
    name: str
    external_id: Optional[str]
    member_ids: Set[str]


def _apply_patch(state: _GroupState, patch: ScimPatchOp) -> None:
    for operation in patch.Operations:
        op = (operation.op or "").strip().lower()
        if op not in ("add", "replace", "remove"):
            raise ScimError(400, f"Unsupported PATCH op '{operation.op}'", "invalidSyntax")
        path = (operation.path or "").strip()
        value = operation.value

        if not path:
            # Okta-style: {"op": "replace", "value": {"id": ..., "displayName": ...}}
            if op == "remove":
                raise ScimError(400, "A remove operation requires a path", "noTarget")
            if not isinstance(value, dict):
                raise ScimError(400, "A PATCH without a path needs an object value", "invalidValue")
            for key, val in value.items():
                _apply_attribute(state, op, key, val)
            continue

        member_match = _MEMBER_VALUE_PATH_RE.match(path)
        if member_match:
            if op != "remove":
                raise ScimError(400, f"'{operation.op}' is not supported on a filtered members path", "invalidPath")
            state.member_ids.discard(_unescape(member_match.group(1)))
            continue

        _apply_attribute(state, op, path, value)


def _apply_attribute(state: _GroupState, op: str, attr: str, value) -> None:
    key = attr.lower()
    if key == "displayname":
        if op == "remove":
            raise ScimError(400, "displayName is required and cannot be removed", "mutability")
        state.name = _display_name(value)
    elif key == "externalid":
        state.external_id = None if op == "remove" else _external_id(value)
    elif key == "members":
        ids = set(_member_ids(value))
        if op == "add":
            state.member_ids |= ids
        elif op == "replace":
            state.member_ids = ids
        elif value is None:  # remove with no value clears the attribute
            state.member_ids = set()
        else:  # Entra: {"op": "Remove", "path": "members", "value": [{"value": id}]}
            state.member_ids -= ids
    elif key in ("id", "schemas", "meta"):
        # Okta echoes the id back in its no-path replace; read-only otherwise.
        return
    else:
        raise ScimError(400, f"Unsupported attribute path '{attr}'", "invalidPath")


def _wants_members(attributes: Optional[str], excluded_attributes: Optional[str]) -> bool:
    """RFC 7644 §3.9 for the one attribute that is worth leaving out."""
    def names(csv: Optional[str]) -> Set[str]:
        return {a.strip().lower().rsplit(":", 1)[-1] for a in (csv or "").split(",") if a.strip()}

    if "members" in names(excluded_attributes):
        return False
    requested = names(attributes)
    if requested and not any(a == "members" or a.startswith("members.") for a in requested):
        return False
    return True


class ScimGroupService:

    # --- reads ---

    def _base_query(self, organization_id: str):
        return (
            select(Group)
            .where(Group.organization_id == organization_id)
            .where(Group.external_provider == PROVIDER_NAME)
            .where(Group.deleted_at.is_(None))
        )

    async def _get(self, db: AsyncSession, organization_id: str, group_id: str) -> Group:
        group = (
            await db.execute(self._base_query(organization_id).where(Group.id == group_id))
        ).scalar_one_or_none()
        if group is None:
            raise ScimError(404, f"Group {group_id} not found")
        return group

    async def _members(
        self, db: AsyncSession, group_ids: List[str]
    ) -> Dict[str, List[Tuple[str, Optional[str]]]]:
        """group_id → [(user_id, display)] for the given groups."""
        out: Dict[str, List[Tuple[str, Optional[str]]]] = {gid: [] for gid in group_ids}
        if not group_ids:
            return out
        rows = await db.execute(
            select(GroupMembership.group_id, User.id, User.name, User.email)
            .join(User, User.id == GroupMembership.user_id)
            .where(GroupMembership.group_id.in_(group_ids))
            .where(GroupMembership.deleted_at.is_(None))
            .order_by(User.email)
        )
        for group_id, user_id, name, email in rows.all():
            out[group_id].append((str(user_id), name or email))
        return out

    @staticmethod
    def _to_scim(
        group: Group,
        members: Optional[List[Tuple[str, Optional[str]]]],
        base_url: str,
    ) -> ScimGroup:
        return ScimGroup(
            id=str(group.id),
            externalId=group.external_id,
            displayName=group.name,
            members=None if members is None else [
                ScimGroupMember(
                    value=user_id,
                    display=display,
                    ref=f"{base_url}/scim/v2/Users/{user_id}",
                    type="User",
                )
                for user_id, display in members
            ],
            meta=ScimMeta(
                resourceType="Group",
                created=group.created_at,
                lastModified=group.updated_at or group.created_at,
                location=f"{base_url}/scim/v2/Groups/{group.id}",
            ),
        )

    async def _render(
        self, db: AsyncSession, group: Group, base_url: str, include_members: bool = True
    ) -> ScimGroup:
        members = (await self._members(db, [str(group.id)]))[str(group.id)] if include_members else None
        return self._to_scim(group, members, base_url)

    async def list_groups(
        self,
        db: AsyncSession,
        organization_id: str,
        filter_str: Optional[str] = None,
        start_index: int = 1,
        count: int = 100,
        attributes: Optional[str] = None,
        excluded_attributes: Optional[str] = None,
        base_url: str = "",
    ) -> ScimGroupListResponse:
        query = self._base_query(organization_id)
        for attr, value in parse_group_filter(filter_str):
            if attr == "displayName":
                query = query.where(func.lower(Group.name) == value.lower())
            elif attr == "externalId":
                query = query.where(Group.external_id == value)
            elif attr == "id":
                query = query.where(Group.id == value)
            elif attr == "members.value":
                query = query.where(Group.id.in_(
                    select(GroupMembership.group_id)
                    .where(GroupMembership.user_id == value)
                    .where(GroupMembership.deleted_at.is_(None))
                ))

        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar()

        groups: List[Group] = []
        if count > 0:
            # Stable order so startIndex paging neither skips nor repeats groups.
            page = query.order_by(Group.created_at, Group.id).offset(start_index - 1).limit(count)
            groups = list((await db.execute(page)).scalars().all())

        include_members = _wants_members(attributes, excluded_attributes)
        members = await self._members(db, [str(g.id) for g in groups]) if include_members else {}
        resources = [
            self._to_scim(g, members.get(str(g.id)) if include_members else None, base_url)
            for g in groups
        ]
        return ScimGroupListResponse(
            totalResults=total,
            startIndex=start_index,
            itemsPerPage=len(resources),
            Resources=resources,
        )

    async def get_group(
        self,
        db: AsyncSession,
        organization_id: str,
        group_id: str,
        attributes: Optional[str] = None,
        excluded_attributes: Optional[str] = None,
        base_url: str = "",
    ) -> ScimGroup:
        group = await self._get(db, organization_id, group_id)
        return await self._render(
            db, group, base_url, include_members=_wants_members(attributes, excluded_attributes)
        )

    # --- writes ---

    async def _check_unique(
        self,
        db: AsyncSession,
        organization_id: str,
        name: str,
        external_id: Optional[str],
        own_id: Optional[str] = None,
    ) -> None:
        # Every group in the org counts, soft-deleted included: UNIQUE
        # (organization_id, name) does not look at deleted_at. Matching a
        # manual/LDAP/OIDC group is a conflict, never an adoption.
        # Case-insensitive, like the displayName filter (caseExact=false): a
        # name differing only in case would otherwise slip past the guard, and
        # two such SCIM groups would both answer the IdP's lookup.
        holders = (
            await db.execute(
                select(Group.id, Group.external_provider)
                .where(Group.organization_id == organization_id)
                .where(func.lower(Group.name) == name.lower())
            )
        ).all()
        holder = next((h for h in holders if str(h[0]) != own_id), None)
        if holder is not None:
            if holder[1] == PROVIDER_NAME:
                detail = f"A SCIM group named '{name}' already exists"
            else:
                detail = f"'{name}' is already used by a group that is not managed by SCIM"
            raise ScimError(409, detail, "uniqueness")

        if external_id:
            clash = (
                await db.execute(
                    self._base_query(organization_id)
                    .where(Group.external_id == external_id)
                    .with_only_columns(Group.id)
                )
            ).scalar_one_or_none()
            if clash is not None and str(clash) != own_id:
                raise ScimError(409, f"A SCIM group with externalId '{external_id}' already exists", "uniqueness")

    async def _check_members(self, db: AsyncSession, organization_id: str, user_ids: Iterable[str]) -> None:
        wanted = set(user_ids)
        if not wanted:
            return
        found = set(
            str(uid) for uid in (
                await db.execute(
                    select(Membership.user_id)
                    .where(Membership.organization_id == organization_id)
                    .where(Membership.deleted_at.is_(None))
                    .where(Membership.user_id.in_(wanted))
                )
            ).scalars().all()
        )
        unknown = sorted(wanted - found)
        if unknown:
            raise ScimError(
                400,
                f"Members must be users of this organization; unknown id(s): {', '.join(unknown)}",
                "invalidValue",
            )

    async def _set_members(self, db: AsyncSession, group_id: str, target: Set[str]) -> None:
        rows = (
            await db.execute(select(GroupMembership).where(GroupMembership.group_id == group_id))
        ).scalars().all()
        by_user = {str(r.user_id): r for r in rows if r.user_id}
        for user_id, row in by_user.items():
            if user_id not in target:
                await db.delete(row)
        for user_id in target:
            row = by_user.get(user_id)
            if row is None:
                db.add(GroupMembership(group_id=group_id, user_id=user_id))
            elif row.deleted_at is not None:
                row.deleted_at = None

    async def _commit(self, db: AsyncSession) -> None:
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent request took the name between our check and commit.
            await db.rollback()
            raise ScimError(409, "The group conflicts with an existing group", "uniqueness") from None

    async def create_group(
        self,
        db: AsyncSession,
        organization_id: str,
        data: ScimGroupCreate,
        base_url: str = "",
    ) -> ScimGroup:
        name = _display_name(data.displayName)
        member_ids = set(_member_ids(data.members))
        await self._check_unique(db, organization_id, name, data.externalId)
        await self._check_members(db, organization_id, member_ids)

        group = Group(
            organization_id=organization_id,
            name=name,
            external_id=data.externalId,
            external_provider=PROVIDER_NAME,
        )
        db.add(group)
        await db.flush()
        await self._set_members(db, str(group.id), member_ids)
        await self._commit(db)
        await db.refresh(group)
        return await self._render(db, group, base_url)

    async def replace_group(
        self,
        db: AsyncSession,
        organization_id: str,
        group_id: str,
        data: ScimGroupCreate,
        base_url: str = "",
    ) -> ScimGroup:
        """PUT — RFC 7644 §3.5.1: omitted readWrite attributes are cleared."""
        group = await self._get(db, organization_id, group_id)
        name = _display_name(data.displayName)
        member_ids = set(_member_ids(data.members))
        await self._check_unique(db, organization_id, name, data.externalId, own_id=str(group.id))
        await self._check_members(db, organization_id, member_ids)

        current = {
            user_id for user_id, _ in (await self._members(db, [str(group.id)]))[str(group.id)]
        }
        group.name = name
        group.external_id = data.externalId
        if member_ids != current:
            await self._set_members(db, str(group.id), member_ids)
            # As in PATCH: a membership-only change leaves the row untouched,
            # so bump it for meta.lastModified.
            group.updated_at = datetime.utcnow()
        await self._commit(db)
        await db.refresh(group)
        return await self._render(db, group, base_url)

    async def patch_group(
        self,
        db: AsyncSession,
        organization_id: str,
        group_id: str,
        patch: ScimPatchOp,
        attributes: Optional[str] = None,
        excluded_attributes: Optional[str] = None,
        base_url: str = "",
    ) -> ScimGroup:
        """PATCH — all operations apply, or none do (RFC 7644 §3.5.2)."""
        group = await self._get(db, organization_id, group_id)
        current = {
            user_id for user_id, _ in (await self._members(db, [str(group.id)]))[str(group.id)]
        }
        state = _GroupState(name=group.name, external_id=group.external_id, member_ids=set(current))
        _apply_patch(state, patch)

        if state.name != group.name or state.external_id != group.external_id:
            await self._check_unique(db, organization_id, state.name, state.external_id, own_id=str(group.id))
        await self._check_members(db, organization_id, state.member_ids - current)

        group.name = state.name
        group.external_id = state.external_id
        if state.member_ids != current:
            await self._set_members(db, str(group.id), state.member_ids)
            # Membership-only patches leave the row untouched; bump it so
            # meta.lastModified reflects the change.
            group.updated_at = datetime.utcnow()
        await self._commit(db)
        await db.refresh(group)
        return await self._render(
            db, group, base_url, include_members=_wants_members(attributes, excluded_attributes)
        )

    async def delete_group(self, db: AsyncSession, organization_id: str, group_id: str) -> None:
        group = await self._get(db, organization_id, group_id)
        # Nothing may keep pointing at the group: report_shares holds a real
        # foreign key (Postgres would refuse the delete), and role assignments,
        # resource grants and quota assignments reference it by principal id,
        # so they would be left as orphans the APIs keep listing.
        await db.execute(delete(ReportShare).where(ReportShare.group_id == group.id))
        await db.execute(
            delete(RoleAssignment)
            .where(RoleAssignment.organization_id == organization_id)
            .where(RoleAssignment.principal_type == "group")
            .where(RoleAssignment.principal_id == group.id)
        )
        await db.execute(
            delete(ResourceGrant)
            .where(ResourceGrant.organization_id == organization_id)
            .where(ResourceGrant.principal_type == "group")
            .where(ResourceGrant.principal_id == group.id)
        )
        await db.execute(
            delete(UsagePolicyAssignment)
            .where(UsagePolicyAssignment.organization_id == organization_id)
            .where(UsagePolicyAssignment.principal_type == "group")
            .where(UsagePolicyAssignment.principal_id == group.id)
        )
        await db.delete(group)  # GroupMemberships go with it (cascade)
        await db.commit()
