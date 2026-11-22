"""A change to a viewer's groups that lands while their run is in flight.

A viewer's run resolves their identity, binds it into the query's identity
params, runs and stores the result with a stamp of what it read
(app/services/viewer_result_freshness.py). If their groups change after the
binding, the stored rows were bound from the identity they had before, and
the stamp must say so: stamped with the new groups, the result would pass for
current and be served as theirs.
"""
import pytest

# An identity param holding the viewer's group names, and step code whose
# rows name them, so the values a run was bound with show in its result.
GROUPS_PARAM = {
    "name": "Groups", "type": "string", "label": "Groups", "default": None, "required": False,
    "source": "identity", "identity_binding": "viewer.groups", "options": None,
    "options_source": None, "strict_options": False,
}


@pytest.fixture
def group_joined_after_bind(monkeypatch):
    """Once armed with {"group", "user"}: right after a run resolved the
    identity its params bind, that user joins that group (in the run's own
    transaction, as a change landing in between would look to it)."""
    from app.models.group_membership import GroupMembership
    from app.services.query_service import QueryService

    original = QueryService._resolve_identity_for
    armed: dict = {}

    async def resolve_then_join(self, db, user, organization_id):
        identity = await original(self, db, user, organization_id)
        if armed:
            db.add(GroupMembership(group_id=armed.pop("group"), user_id=armed.pop("user")))
            await db.flush()
        return identity

    monkeypatch.setattr(QueryService, "_resolve_identity_for", resolve_then_join)
    return armed
