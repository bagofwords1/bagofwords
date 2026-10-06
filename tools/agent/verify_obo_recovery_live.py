"""Live demo-tenant OBO recovery. Secrets enter hidden stdin, never source/logs."""

import asyncio
import getpass
import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, os.getcwd())
import httpx

CLIENT_ID = str(uuid.UUID(os.environ["BOW_LIVE_CLIENT_ID"]))
TENANT_ID = str(uuid.UUID(os.environ["BOW_LIVE_TENANT_ID"]))
SCOPE = os.environ.get(
    "BOW_LIVE_LOGIN_SCOPE",
    f"openid profile email offline_access api://{CLIENT_ID}/access_as_user",
)
ENDPOINT = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
PBI = "https://api.powerbi.com/v1.0/myorg"


def emit(stage, **fields):
    print(json.dumps({"stage": stage, **fields}), flush=True)


async def drain_worker():
    from app.services.connection_indexing_service import _get_background_loop

    async def drain():
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks)

    await asyncio.wait_for(
        asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(drain(), _get_background_loop())
        ),
        150,
    )


async def probe_powerbi(label, token):
    async with httpx.AsyncClient(
        timeout=30, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        groups = await client.get(PBI + "/groups")
        group_rows = groups.json().get("value", []) if groups.status_code == 200 else []
        emit(
            label + ":powerbi_workspaces",
            http_status=groups.status_code,
            count=len(group_rows),
        )
        models = await client.get(PBI + "/datasets")
        model_rows = models.json().get("value", []) if models.status_code == 200 else []
        emit(
            label + ":powerbi_datasets",
            http_status=models.status_code,
            count=len(model_rows),
        )
        targets = [(None, row["id"]) for row in model_rows]
        for group in group_rows[:5]:
            response = await client.get(PBI + f"/groups/{group['id']}/datasets")
            if response.status_code == 200:
                targets.extend(
                    (group["id"], row["id"]) for row in response.json().get("value", [])
                )
        seen = set()
        for group_id, model_id in targets:
            if model_id in seen:
                continue
            seen.add(model_id)
            route = (
                f"/groups/{group_id}/datasets/{model_id}/executeQueries"
                if group_id
                else f"/datasets/{model_id}/executeQueries"
            )
            response = await client.post(
                PBI + route,
                json={
                    "queries": [{"query": 'EVALUATE ROW("connection_verified", 1)'}],
                    "serializerSettings": {"includeNulls": True},
                },
            )
            body = response.json()
            error = body.get("error") or next(
                (r.get("error") for r in body.get("results", []) if r.get("error")),
                None,
            )
            success = response.status_code == 200 and not error
            emit(
                label + ":powerbi_dax",
                http_status=response.status_code,
                success=success,
                error_code=error.get("code") if isinstance(error, dict) else None,
            )
            if success:
                rows = body["results"][0]["tables"][0]["rows"]
                assert (
                    rows
                    and rows[0].get(
                        "[connection_verified]", rows[0].get("connection_verified")
                    )
                    == 1
                )
                return True
            if len(seen) >= 5:
                break
        return False


async def run():
    import main  # noqa: F401 — register all ORM models
    from cryptography.fernet import Fernet
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.settings.config import settings
    from app.settings.bow_config import OIDCProvider
    from app.dependencies import async_session_maker, engine
    from app.models.user import User
    from app.models.organization import Organization
    from app.models.membership import Membership
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.domain_connection import domain_connection
    from app.models.report import Report
    from app.models.oauth_account import OAuthAccount
    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.models.user_data_source_overlay import UserDataSourceTable
    from app.ai.tools.implementations.agent_focus_common import prepare_run_agents
    from app.services.connection_identity import build_token_identity_status
    from app.services.connection_service import ConnectionService
    from app.services.connection_indexing_service import shutdown_background_loop

    logging.disable(logging.CRITICAL)
    settings.TESTING = True
    settings.bow_config.encryption_key = Fernet.generate_key().decode()
    settings.bow_config.telemetry.enabled = False
    settings.bow_config.oidc_providers = [
        OIDCProvider(
            name="live_entra",
            enabled=True,
            issuer=f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
            client_id=CLIENT_ID,
            client_secret=os.environ["BOW_LIVE_CLIENT_SECRET"],
            scopes=SCOPE.split(),
        )
    ]
    async with async_session_maker() as db:
        org = Organization(
            name="Isolated live Power BI recovery " + uuid.uuid4().hex[:8]
        )
        db.add(org)
        await db.flush()
        conn = Connection(
            name="Live Power BI",
            type="powerbi",
            organization_id=org.id,
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
            config={},
        )
        conn.encrypt_credentials(
            {
                "tenant_id": TENANT_ID,
                "client_id": CLIENT_ID,
                "client_secret": os.environ["BOW_LIVE_CLIENT_SECRET"],
            }
        )
        db.add(conn)
        await db.flush()
        ds = DataSource(
            name="Public Power BI verification", organization_id=org.id, is_public=True
        )
        db.add(ds)
        await db.flush()
        await db.execute(
            domain_connection.insert().values(
                data_source_id=ds.id, connection_id=conn.id
            )
        )
        await db.commit()
        org_id, conn_id, ds_id = str(org.id), str(conn.id), str(ds.id)

    async with httpx.AsyncClient(timeout=30) as http:
        usernames = [
            u.strip() for u in os.environ["BOW_LIVE_USERS"].split(",") if u.strip()
        ]
        if len(usernames) != 2:
            raise ValueError("BOW_LIVE_USERS must specify exactly two demo usernames")
        cases = [
            ("user1_access", usernames[0], False),
            ("user2_refresh", usernames[1], True),
            ("invalid_tokens", None, False),
        ]
        for label, username, force_refresh in cases:
            if username:
                response = await http.post(
                    ENDPOINT,
                    data={
                        "grant_type": "password",
                        "client_id": CLIENT_ID,
                        "client_secret": os.environ["BOW_LIVE_CLIENT_SECRET"],
                        "username": username,
                        "password": os.environ["BOW_LIVE_USER_PASSWORD"],
                        "scope": SCOPE,
                    },
                )
                body = response.json()
                emit(
                    label + ":entra_login",
                    http_status=response.status_code,
                    error_codes=body.get("error_codes", []),
                )
                assert response.status_code == 200, "Live delegated login failed"
            else:
                body = {
                    "access_token": "invalid-live-assertion",
                    "refresh_token": "invalid-live-refresh",
                    "expires_in": 3600,
                }
            async with async_session_maker() as db:
                user = User(
                    name=label,
                    email=f"{label}-{uuid.uuid4().hex}@example.invalid",
                    hashed_password="unused",
                    is_active=True,
                )
                db.add(user)
                await db.flush()
                db.add(
                    Membership(user_id=user.id, organization_id=org_id, role="member")
                )
                db.add(
                    OAuthAccount(
                        user_id=user.id,
                        oauth_name="live_entra",
                        account_id=str(uuid.uuid4()),
                        account_email=username or user.email,
                        access_token=body["access_token"],
                        refresh_token=body.get("refresh_token"),
                        expires_at=int(time.time())
                        + (-60 if force_refresh else body.get("expires_in", 3600)),
                    )
                )
                org = await db.get(Organization, org_id)
                ds = await db.scalar(
                    select(DataSource)
                    .where(DataSource.id == ds_id)
                    .options(selectinload(DataSource.connections))
                )
                report = Report(
                    title="Live delegated recovery",
                    slug=uuid.uuid4().hex,
                    organization_id=org_id,
                    user_id=user.id,
                    data_sources=[ds],
                )
                db.add(report)
                await db.commit()
                user_id, report_id = str(user.id), str(report.id)
                _, clients = await prepare_run_agents(db, org, user, report)
                emit(
                    label + ":sse_prepare_before",
                    client_count=len(clients),
                    forced_refresh=force_refresh,
                )
            assert not clients, "Fixture must start without delegated credentials"
            await drain_worker()
            async with async_session_maker() as db:
                rows = (
                    await db.scalars(
                        select(UserConnectionCredentials).where(
                            UserConnectionCredentials.user_id == user_id,
                            UserConnectionCredentials.connection_id == conn_id,
                            UserConnectionCredentials.is_active.is_(True),
                        )
                    )
                ).all()
                overlays = (
                    await db.scalars(
                        select(UserDataSourceTable).where(
                            UserDataSourceTable.user_id == user_id,
                            UserDataSourceTable.connection_id == conn_id,
                            UserDataSourceTable.is_accessible.is_(True),
                        )
                    )
                ).all()
                conn = await db.get(Connection, conn_id)
                user = await db.get(User, user_id)
                status = await build_token_identity_status(db, conn, user)
                emit(
                    label + ":recovery_result",
                    credential_count=len(rows),
                    accessible_catalog_tables=len(overlays),
                    effective_auth=status.effective_auth,
                )
                if username:
                    assert (
                        len(rows) == 1 and overlays and status.effective_auth == "user"
                    ), "Recovery failed"
                    if force_refresh:
                        account = await db.scalar(
                            select(OAuthAccount).where(OAuthAccount.user_id == user_id)
                        )
                        assert account.expires_at > time.time(), (
                            "Login token was not refreshed"
                        )
                        emit(label + ":login_refresh", expiry_renewed=True)
                    creds = await ConnectionService().resolve_credentials(
                        db, conn, user
                    )
                    org = await db.get(Organization, org_id)
                    report = await db.scalar(
                        select(Report)
                        .where(Report.id == report_id)
                        .options(
                            selectinload(Report.data_sources).selectinload(
                                DataSource.connections
                            )
                        )
                    )
                    _, clients = await prepare_run_agents(db, org, user, report)
                    emit(label + ":sse_prepare_after", client_count=len(clients))
                    assert clients, "Completion preparation still has no usable client"
                    assert await probe_powerbi(label, creds["access_token"]), (
                        "No successful live DAX query"
                    )
                else:
                    assert (
                        not rows and not overlays and status.effective_auth == "none"
                    ), "Invalid tokens must not grant access"
                    emit(
                        label + ":fail_closed",
                        passed=(
                            label == "invalid_tokens"
                            and status.effective_auth == "none"
                        ),
                    )
            await drain_worker()
    shutdown_background_loop()
    await engine.dispose()


if __name__ == "__main__":
    from urllib.parse import urlsplit

    target = urlsplit(os.environ.get("TEST_DATABASE_URL", ""))
    if os.environ.get("TESTING", "").lower() != "true" or target.hostname not in {
        "localhost",
        "127.0.0.1",
    }:
        raise SystemExit(
            "Requires TESTING=true and a disposable localhost TEST_DATABASE_URL"
        )
    os.environ["BOW_LIVE_CLIENT_SECRET"] = os.environ.get(
        "BOW_LIVE_CLIENT_SECRET"
    ) or getpass.getpass("Client secret (hidden): ")
    os.environ["BOW_LIVE_USER_PASSWORD"] = os.environ.get(
        "BOW_LIVE_USER_PASSWORD"
    ) or getpass.getpass("Demo password (hidden): ")
    try:
        asyncio.run(run())
    except Exception as exc:
        root = getattr(getattr(exc, "orig", None), "__cause__", None)
        emit(
            "live_test_error",
            exception=type(exc).__name__,
            sqlstate=getattr(root, "sqlstate", None),
            column=getattr(root, "column_name", None),
            table=getattr(root, "table_name", None),
            constraint=getattr(root, "constraint_name", None),
        )
        import traceback

        for frame in traceback.extract_tb(exc.__traceback__)[-5:]:
            emit(
                "error_location",
                file=frame.filename,
                line=frame.lineno,
                function=frame.name,
            )
        sys.exit(1)
    finally:
        os.environ.pop("BOW_LIVE_CLIENT_SECRET", None)
        os.environ.pop("BOW_LIVE_USER_PASSWORD", None)
