"""member_mapped identity: binding validation and the shared-data protections
that per-user auth brings with it.

The review of the first design found that mapped connections were saved as
``system_only``, so a viewer of a shared report saw the creator's saved rows.
Mapped identity now lives under ``user_required``; these tests pin that the
snapshot-withholding policy treats such a connection as per-user.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.errors import AppError
from app.models.base import BaseSchema
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.services.member_identity import (
    member_mapped,
    resolve_bound_values,
    validate_member_bindings,
)
from app.services.viewer_data_policy import snapshot_withheld_for_viewers, sources_credential_scoped


class TestValidation:
    def test_qlik_identity_fields_bind_and_blank_keys_unbind(self):
        out = validate_member_bindings(
            "qlik_sense_onprem", None, {"user_id": " qlik_user_id ", "user_directory": ""}
        )
        assert out == {"user_id": "qlik_user_id"}

    @pytest.mark.parametrize("ds_type,bindings", [
        ("qlik_sense_onprem", {"client_cert": "x"}),
        ("qlik_sense_onprem", {"user_id": "1bad"}),
        ("qlik_sense_onprem", {"user_id": "a" * 65}),
        ("postgresql", {"user": "pg_role"}),
        ("postgresql", {"password": "pg_role"}),
    ])
    def test_everything_else_is_refused(self, ds_type, bindings):
        with pytest.raises(AppError) as exc:
            validate_member_bindings(ds_type, None, bindings)
        assert exc.value.error_code == "member_identity.invalid"

    def test_only_user_required_with_the_mode_is_mapped(self):
        conn = Connection(auth_policy="user_required", allowed_user_auth_modes=["member_mapped"])
        assert member_mapped(conn)
        conn.auth_policy = "system_only"
        assert not member_mapped(conn)
        assert not member_mapped(Connection(auth_policy="user_required", allowed_user_auth_modes=["oauth"]))

    def test_values_are_trimmed_and_non_strings_coerced(self):
        values, missing = resolve_bound_values(
            {"user_id": "id", "user_directory": "dir"}, {"id": 1042, "dir": " CORP "}
        )
        assert values == {"user_id": "1042", "user_directory": "CORP"}
        assert missing == []


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_mapped_qlik(db):
    org = Organization(name="o")
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add_all([org, user])
    await db.flush()
    report = Report(
        title="r", user_id=str(user.id), organization_id=str(org.id),
        slug="r1", status="published", shared_run_identity="viewer",
    )
    conn = Connection(
        name="qlik-1", type="qlik_sense_onprem", config="{}", organization_id=str(org.id),
        is_active=True, auth_policy="user_required", allowed_user_auth_modes=["member_mapped"],
        member_bindings={"user_id": "qlik_user_id"},
    )
    ds = DataSource(name="Sales", organization_id=str(org.id), owner_user_id=str(user.id))
    ds.connections.append(conn)
    db.add_all([report, conn, ds])
    await db.flush()
    return org, report


CODE = "def generate_df(ds_clients, excel_files):\n    ds_clients['Sales:qlik-1'].execute_query('x')"


@pytest.mark.asyncio
async def test_viewers_never_see_the_creators_rows_of_a_mapped_connection(db):
    org, report = await _seed_mapped_qlik(db)
    assert await snapshot_withheld_for_viewers(
        db, str(report.id), "viewer", fallback_org_id=str(org.id), code=CODE,
    ) is True
    # "View as" cannot reproduce another member's rows either.
    assert await sources_credential_scoped(
        db, str(report.id), fallback_org_id=str(org.id), code=CODE,
    ) is True
