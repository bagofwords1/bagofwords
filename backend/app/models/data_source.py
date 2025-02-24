from sqlalchemy import Column, String, UUID, Boolean, Enum, JSON, DateTime, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from sqlalchemy import ForeignKey
from app.models.user import User
from app.models.organization import Organization
from importlib import import_module
from cryptography.fernet import Fernet
from app.settings.config import settings
import json
from typing import List
from app.models.datasource_table import DataSourceTable
from app.schemas.datasource_table_schema import DataSourceTableSchema
from app.ai.prompt_formatters import Table, TableColumn
from sqlalchemy.orm import selectinload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import object_session


DATA_SOURCE_DETAILS = [
    {
        "type": "postgresql",
        "title": "PostgreSQL",
        "description": "Open-source relational database known for reliability and feature robustness.",
        "config": "PostgreSQLConfig",
        "status": "active",
        "version": "1.0.0"
    },
    {
        "type": "snowflake",
        "title": "Snowflake",
        "description": "Cloud-based data warehousing platform that supports SQL queries.",
        "config": "SnowflakeConfig",
        "status": "active",
        "version": "1.0.0"
    },
    {
        "type": "bigquery",
        "title": "Google BigQuery",
        "description": "Serverless, highly scalable, and cost-effective multi-cloud data warehouse.",
        "config": "BigQueryConfig",
        "status": "active",
        "version": "1.0.0"
    },
    {
        "type": "netsuite",
        "title": "NetSuite",
        "description": "Cloud-based enterprise resource planning (ERP) software suite.",
        "config": "NetSuiteConfig",
        "status": "inactive",
        "version": "0.0.0"
    },
    {
        "type": "mysql",
        "title": "MySQL",
        "description": "Popular open-source relational database management system.",
        "config": "SQLConfig",
        "status": "active",
        "version": "1.0.0"
    },

    {
        "type": "mariadb",
        "title": "Mariadb",
        "description": "MariaDB is a fast, open-source MySQL replacement.",
        "config": "SQLConfig",
        "status": "active",
        "version": "1.0.0"
    },
    {
        "type": "salesforce",
        "title": "Salesforce",
        "description": "Cloud-based CRM platform for sales, service, marketing, and more.",
        "config": "SalesforceConfig",
        "status": "active",
        "version": "1.0.0"
    },

    {
        "type": "MSSQL",
        "title": "Microsoft SQL Server",
        "description": "MSSQL is Microsoft's relational database for managing and analyzing data.",
        "config": "SQLConfig",
        "status": "active",
        "version": "1.0.0"
    },

    {
        "type": "clickhouse",
        "title": "ClickHouse",
        "description": "ClickHouse is a fast, open-source columnar database for real-time analytics.",
        "config": "ClickhouseConfig",
        "status": "active",
        "version": "1.0.0"
    },

    {
        "type": "aws_cost",
        "title": "AWS Cost Explorer",
        "description": "AWS Cost Explorer helps analyze and visualize your AWS spending and usage patterns over time.",
        "config": "AWSCostConfig",
        "status": "active",
        "version": "beta"
    }


]


class DataSource(BaseSchema):
    __tablename__ = "data_sources"

    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    config = Column(JSON, nullable=False)  # Stores the JSON config
    credentials = Column(Text, nullable=True)  # Stores the credentials
    last_synced_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    context = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    conversation_starters = Column(JSON, nullable=True)

    # The organization that owns this data source
    organization_id = Column(String(36), ForeignKey(
        'organizations.id'), nullable=False)
    organization = relationship("Organization", back_populates="data_sources")
    reports = relationship(
        "Report", secondary="report_data_source_association", back_populates="data_sources")
    tables = relationship("DataSourceTable", back_populates="datasource")

    def get_client(self):
        try:
            module_name = f"app.data_sources.clients.{self.type.lower()}_client"
            # Capitalize the first letter of each word without changing the rest of the word's case
            title = "".join(word[:1].upper() + word[1:] for word in self.type.split("_"))
            class_name = f"{title}Client"
            
            module = import_module(module_name)
            ClientClass = getattr(module, class_name)
            
            # Parse config if it's a string
            config = json.loads(self.config) if isinstance(self.config, str) else self.config
            client_params = config.copy()
            
            # Only decrypt and merge credentials if they exist
            if self.credentials:
                decrypted_credentials = self.decrypt_credentials()
                client_params.update(decrypted_credentials)
            
            # Initialize client with parameters
            return ClientClass(**client_params)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Unable to load data source client for {self.type}: {str(e)}")
        
    def encrypt_credentials(self, credentials: dict):
        fernet = Fernet(settings.bow_config.encryption_key)
        self.credentials = fernet.encrypt(json.dumps(credentials).encode()).decode()

    def decrypt_credentials(self) -> dict:
        fernet = Fernet(settings.bow_config.encryption_key)
        return json.loads(fernet.decrypt(self.credentials.encode()).decode())

    async def get_schemas(self, db: AsyncSession = None, include_inactive: bool = False) -> List[Table]:
        """
        Get the database schema information from associated DataSourceTable records.
        Returns a list of Table objects containing table structure information.
        """
        # Use provided session or try to get from object
        session = db or object_session(self)
        if not isinstance(session, AsyncSession):
            raise RuntimeError("An async database session is required")
            
        # Load the data source with its tables
        stmt = select(DataSource).where(DataSource.id == self.id).options(selectinload(DataSource.tables))
        result = await session.execute(stmt)
        data_source = result.scalar_one()
        
        tables = []
        for table in data_source.tables:
            if not include_inactive and not table.is_active:
                continue
                
            columns = [
                TableColumn(name=col["name"], dtype=col.get("dtype", "unknown"))
                for col in table.columns
            ]
            
            tables.append(Table(
                name=table.name,
                columns=columns,
                pks=table.pks,
                fks=table.fks,
                is_active=table.is_active
            ))
            
        return tables

    async def prompt_schema(self, db: AsyncSession = None) -> str:
        """
        Get the database schema information using TableFormatter.
        Returns a formatted string suitable for prompts.
        """
        from app.ai.prompt_formatters import TableFormatter
        # Pass the session to get_schemas
        tables = await self.get_schemas(db=db)
        return TableFormatter(tables).table_str