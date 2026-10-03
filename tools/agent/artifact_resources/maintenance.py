"""Run from backend with its Python environment and production configuration."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
import main as _application  # noqa: F401 — register the complete model graph
from app.dependencies import async_session_maker
from app.services.artifact_maintenance import maintain


async def main():
    async with async_session_maker() as db:
        print(json.dumps(await maintain(db)))


if __name__ == "__main__":
    asyncio.run(main())
