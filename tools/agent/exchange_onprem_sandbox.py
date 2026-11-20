#!/usr/bin/env python3
"""Backend for a live run of the Exchange Server (on-prem) mail connector.

Run from backend/ with its venv, next to tools/agent/mock_exchange_server.py:

    REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem \\
    BOW_ENCRYPTION_KEY=<fernet key> \\
    uv run python ../tools/agent/exchange_onprem_sandbox.py

Boots the real app on a dedicated SQLite database with an in-process enterprise
license, because the connector is enterprise-gated and no license override is
shipped. Everything else is the stock application.

REQUESTS_CA_BUNDLE must hold the mock server's certificate (it writes one with
--cert-out) so the connector can verify TLS; append the environment's own
bundle to it when the backend also has to reach an LLM provider.
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
DB = Path(os.environ.get("BOW_SANDBOX_DB") or BACKEND / "db" / "exchange_onprem_sandbox.db").resolve()
PORT = int(os.environ.get("BOW_SANDBOX_PORT", "8000"))


def main():
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("BOW_DATABASE_URL", f"sqlite:///{DB}")
    from app.ee import license as lic

    lic._get_license_key = lambda: "sandbox"
    lic._validate_license_key = lambda key: lic.LicenseInfo(
        licensed=True, tier="enterprise", org_name="exchange-onprem-sandbox", features=["audit_logs"],
    )
    lic.clear_license_cache()

    from alembic import command
    from alembic.config import Config
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")

    import uvicorn
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
