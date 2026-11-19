"""What the prompt lists vs what saved code can reach, for the renamed agent."""
import asyncio, main  # noqa: F401  (registers the mappers)
from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.services.data_source_service import DataSourceService
from app.services.client_keys import with_former_keys
from app.services.viewer_data_policy import _org_data_sources, _sources_reached

async def go():
    async with async_session_maker() as db:
        ds = (await db.execute(select(DataSource).where(DataSource.name == "Sales"))).scalars().unique().one()
        clients = await DataSourceService().construct_clients(db, ds, None)
        print("prompt lists      :", sorted(clients))
        print("saved code reaches:", sorted(with_former_keys(clients)))
        rows = await _org_data_sources(db, ds.organization_id)
        code = "def generate_df(ds_clients, excel_files):\n    return ds_clients['Sales DWH:sqlite-2'].execute_query('select 1')\n"
        print("policy: old-key code runs on", _sources_reached(rows, code) == [str(ds.id)] and "the renamed agent only" or _sources_reached(rows, code))
asyncio.run(go())
