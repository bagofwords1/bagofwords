"""Opt-in live service verification with separate Entra SSO/Power BI apps.

Run from backend with Python 3.12. See docs/feedback-loops/entra-directory-token-isolation.md.
Uses a disposable SQLite database; never reads or changes a deployed BOW database.
All credentials come from environment variables. No Entra configuration is modified.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def required(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing environment variable: {name}")
    return value


async def verify():
    # Import only after main() installs the isolated BOW configuration.
    from sqlalchemy import select

    import main  # noqa: F401
    from app.dependencies import async_session_maker
    from app.ee.oidc.profile_service import fetch_profile_fields, sync_profile_on_login
    from app.models.base import Base
    from app.models.connection import Connection
    from app.models.membership import Membership
    from app.models.oauth_account import OAuthAccount
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.services.connection_oauth_service import exchange_obo_token
    from app.services.obo_recovery_service import recover_stored_login

    # Old code logs provider bodies. Do not expose those when reproducing it.
    logging.disable(logging.CRITICAL)
    assertion = required("ENTRA_TEST_ACCESS_TOKEN")
    refresh = required("ENTRA_TEST_REFRESH_TOKEN")
    results = {"separate_clients": True}
    async with async_session_maker() as db:
        async with db.bind.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        user = User(
            name="Live verification", email="verification@example.invalid", hashed_password="unused", is_active=True
        )
        org = Organization(name="Disposable verification")
        db.add_all([user, org])
        await db.flush()
        membership = Membership(user_id=user.id, organization_id=org.id, role="member")
        expiry = int(time.time()) + 3600
        account = OAuthAccount(
            user_id=user.id,
            oauth_name="entra",
            account_id="live-demo",
            account_email=user.email,
            access_token=assertion,
            refresh_token=refresh,
            expires_at=expiry,
        )
        db.add_all([membership, account])
        await db.commit()
        fields = ["jobTitle", "department", "companyName", "officeLocation", "employeeId"]
        try:
            attrs = await sync_profile_on_login(db, user, org.id, fields, assertion)
            await db.refresh(membership)
            results["directory_sync"] = "PASS" if attrs and membership.profile_attributes == attrs else "EMPTY"
            results["directory_fields"] = sorted(attrs)
        except Exception as exc:
            results.update(directory_sync="FAIL", directory_error=type(exc).__name__)
        await db.refresh(account)
        results["assertion_preserved"] = account.access_token == assertion and account.expires_at == expiry
        conn = Connection(
            name="Power BI verification",
            type="powerbi",
            organization_id=org.id,
            config={},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
        )
        conn.encrypt_credentials(
            {
                "tenant_id": required("ENTRA_TEST_TENANT_ID"),
                "client_id": required("ENTRA_TEST_FABRIC_CLIENT_ID"),
                "client_secret": required("ENTRA_TEST_FABRIC_CLIENT_SECRET"),
            }
        )
        db.add(conn)
        await db.commit()
        url = (
            "https://api.powerbi.com/v1.0/myorg/groups/"
            + required("ENTRA_TEST_PBI_GROUP_ID")
            + "/datasets/"
            + required("ENTRA_TEST_PBI_DATASET_ID")
            + "/executeQueries"
        )

        async def query(access):
            async with httpx.AsyncClient(timeout=40) as http:
                response = await http.post(
                    url,
                    headers={"Authorization": "Bearer " + access},
                    json={"queries": [{"query": 'EVALUATE ROW("Verified", 1)'}]},
                )
                return {
                    "http": response.status_code,
                    "value": response.json()["results"][0]["tables"][0]["rows"][0].get("[Verified]")
                    if response.status_code == 200
                    else None,
                }

        delegated = await exchange_obo_token(account.access_token, conn)
        results["powerbi_after_profile"] = await query(delegated["access_token"])
        if results["directory_sync"] == "PASS":
            again = await fetch_profile_fields(db, user, fields)
            await db.refresh(account)
            results["repeat_profile"] = bool(again) and account.access_token == assertion
            # Force the stored assertion's expiry metadata past due: recovery
            # must use the SSO refresh token after directory sync rotated it.
            account.expires_at = int(time.time()) - 60
            await db.commit()
            await recover_stored_login(db, user, organization_id=org.id)
            credential = await db.scalar(
                select(UserConnectionCredentials).where(
                    UserConnectionCredentials.user_id == user.id,
                    UserConnectionCredentials.connection_id == conn.id,
                    UserConnectionCredentials.is_active.is_(True),
                )
            )
            results["credential_recovered_after_expiry"] = credential is not None
            if credential:
                results["powerbi_after_recovery"] = await query(credential.decrypt_credentials()["access_token"])
            results["directory_after_recovery"] = bool(await fetch_profile_fields(db, user, fields))
        print(json.dumps(results, indent=2))
        expected = {"http": 200, "value": 1}
        assert results["directory_sync"] == "PASS", "Directory sync failed (see sanitized results)"
        assert results["assertion_preserved"] and results["repeat_profile"]
        assert results["powerbi_after_profile"] == expected
        assert results["credential_recovered_after_expiry"]
        assert results["powerbi_after_recovery"] == expected and results["directory_after_recovery"]
    await db.bind.dispose()


def main():
    from cryptography.fernet import Fernet

    sso = required("ENTRA_TEST_SSO_CLIENT_ID")
    fabric = required("ENTRA_TEST_FABRIC_CLIENT_ID")
    assert sso != fabric, "This verification requires two different app registrations"
    with tempfile.TemporaryDirectory(prefix="bow-directory-verification-") as directory:
        os.environ["BOW_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        config = {
            "database": {"url": "sqlite:///" + directory + "/app.db"},
            "encryption_key": "${BOW_ENCRYPTION_KEY}",
            "telemetry": {"enabled": False},
            "oidc_providers": [
                {
                    "name": "entra",
                    "enabled": True,
                    "client_id": sso,
                    "client_secret": "${ENTRA_TEST_SSO_CLIENT_SECRET}",
                    "issuer": "https://login.microsoftonline.com/" + required("ENTRA_TEST_TENANT_ID") + "/v2.0",
                    "scopes": ["openid", "profile", "email", "offline_access", required("ENTRA_TEST_API_SCOPE")],
                }
            ],
        }
        path = Path(directory) / "bow.yaml"
        path.write_text(yaml.safe_dump(config))
        os.environ["BOW_CONFIG_PATH"] = str(path)
        asyncio.run(verify())


if __name__ == "__main__":
    main()
