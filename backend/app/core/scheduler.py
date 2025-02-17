from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from app.settings.database import create_database_engine

# Use synchronous engine directly
jobstore = SQLAlchemyJobStore(
    engine=create_database_engine(),
    tablename='apscheduler_jobs'
)

scheduler = AsyncIOScheduler(
    jobstores={
        'default': jobstore
    }
)
