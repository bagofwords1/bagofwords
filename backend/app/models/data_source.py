from sqlalchemy import Column, String, UUID, Boolean, Enum, JSON, DateTime, Text, select
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
from app.ai.context.resource_context_builder import ResourceContextBuilder


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
        "type": "aws_athena",
        "title": "AWS Athena",
        "description": "AWS Athena is a serverless query service that makes it easy to analyze data in Amazon S3 using standard SQL.",
        "config": "AWSAthenaConfig",
        "status": "active",
        "version": "beta"
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
    },

    {
        "type": "vertica",
        "title": "Vertica",
        "description": "High-performance columnar analytics database optimized for large-scale data warehousing and analytics workloads.",
        "config": "VerticaConfig",
        "status": "active",
        "version": "1.0.0"
    },

    {
        "type": "aws_redshift",
        "title": "AWS Redshift",
        "description": "Fully managed, petabyte-scale data warehouse service in the cloud for analytics and business intelligence.",
        "config": "AwsRedshiftConfig",
        "status": "active",
        "version": "1.0.0"
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
    is_public = Column(Boolean, nullable=False, default=True)
    owner_user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    context = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    conversation_starters = Column(JSON, nullable=True)

    # The organization that owns this data source
    organization_id = Column(String(36), ForeignKey(
        'organizations.id'), nullable=False)
    organization = relationship("Organization", back_populates="data_sources")
    owner = relationship("User", foreign_keys=[owner_user_id])
    reports = relationship(
        "Report", 
        secondary="report_data_source_association", 
        back_populates="data_sources",
        lazy="selectin"
    )
    tables = relationship("DataSourceTable", back_populates="datasource")
    git_repository = relationship(
        "GitRepository", 
        back_populates="data_source", 
        uselist=False,
        lazy="selectin",
        overlaps="reports,organization"
    )
    metadata_resources = relationship("MetadataResource", back_populates="data_source")
    metadata_indexing_jobs = relationship("MetadataIndexingJob", back_populates="data_source")
    data_source_memberships = relationship("DataSourceMembership", back_populates="data_source", cascade="all, delete-orphan")

    @property
    def memberships(self):
        """Alias for data_source_memberships to match schema expectations"""
        return self.data_source_memberships

    instructions = relationship(
    "Instruction", 
    secondary="instruction_data_source_association", 
    back_populates="data_sources",
    lazy="selectin")
    
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
            
            # Debug logging
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Client params for {self.type}: {client_params}")
            logger.info(f"Config keys: {list(client_params.keys())}")
            logger.info(f"Workgroup name in params: {client_params.get('workgroup_name')}")
            logger.info(f"Cluster identifier in params: {client_params.get('cluster_identifier')}")
            
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

    async def get_schemas(self, db: AsyncSession = None, include_inactive: bool = False, with_stats: bool = False, organization: Organization | None = None, top_k: int | None = None) -> List[Table]:
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
        # Prepare stats if requested
        breakpoint()
        stats_map = {}
        if with_stats and organization is not None:
            from app.models.table_stats import TableStats
            stats_rows = await session.execute(
                select(TableStats).where(
                    TableStats.org_id == organization.id,
                    TableStats.report_id == None,
                    TableStats.data_source_id == self.id,
                )
            )
            stats_rows = stats_rows.scalars().all()
            for s in stats_rows:
                stats_map[(s.table_fqn or '').lower()] = s

        scored: list[tuple[float, Table]] = []
        for table in data_source.tables:
            if not include_inactive and not table.is_active:
                continue
                
            columns = [
                TableColumn(name=col["name"], dtype=col.get("dtype", "unknown"))
                for col in table.columns
            ]
            
            tbl = Table(
                name=table.name,
                columns=columns,
                pks=table.pks,
                fks=table.fks,
                is_active=table.is_active,
                centrality_score=table.centrality_score,
                richness=table.richness,
                degree_in=table.degree_in,
                degree_out=table.degree_out,
                entity_like=table.entity_like,
            )
            if with_stats and organization is not None:
                key = (table.name or '').lower()
                s = stats_map.get(key)
                # Compute simple score and attach stats if present
                if s:
                    usage_count = int(s.usage_count or 0)
                    success_count = int(s.success_count or 0)
                    failure_count = int(s.failure_count or 0)
                    weighted_usage_count = float(s.weighted_usage_count or 0.0)
                    pos_feedback_count = int(s.pos_feedback_count or 0)
                    neg_feedback_count = int(s.neg_feedback_count or 0)
                    last_used_at = s.last_used_at.isoformat() if s.last_used_at else None
                    last_feedback_at = s.last_feedback_at.isoformat() if s.last_feedback_at else None
                    success_rate = (success_count / max(1, usage_count)) if usage_count > 0 else 0.0
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc)
                    if s.last_used_at:
                        age_days = max(0.0, (now - s.last_used_at.replace(tzinfo=timezone.utc)).total_seconds() / 86400.0)
                    else:
                        age_days = 365.0
                    recency = pow(2.718281828, -age_days / 14.0)
                    usage_signal = (weighted_usage_count)**0.5
                    feedback_signal = (float(s.weighted_pos_feedback or 0.0) - float(s.weighted_neg_feedback or 0.0))
                    structural_signal = (float(table.centrality_score or 0.0) + float(table.richness or 0.0) + (0.5 if table.entity_like else 0.0))
                    score = 0.35 * (usage_signal * recency) + 0.25 * success_rate + 0.2 * feedback_signal + 0.2 * structural_signal - 0.2 * (failure_count**0.5)
                    tbl.usage_count = usage_count
                    tbl.success_count = success_count
                    tbl.failure_count = failure_count
                    tbl.weighted_usage_count = weighted_usage_count
                    tbl.pos_feedback_count = pos_feedback_count
                    tbl.neg_feedback_count = neg_feedback_count
                    tbl.last_used_at = last_used_at
                    tbl.last_feedback_at = last_feedback_at
                    tbl.success_rate = round(success_rate, 4)
                    tbl.score = float(round(score, 6))
                    scored.append((tbl.score or 0.0, tbl))
                else:
                    structural_signal = (float(table.centrality_score or 0.0) + float(table.richness or 0.0) + (0.5 if table.entity_like else 0.0))
                    score = 0.1 * structural_signal
                    tbl.score = float(round(score, 6))
                    scored.append((tbl.score or 0.0, tbl))
            else:
                tables.append(tbl)

        if with_stats and organization is not None:
            scored.sort(key=lambda x: x[0], reverse=True)
            tables = [t for (_, t) in scored]
            if top_k is not None and top_k > 0:
                tables = tables[:top_k]
            
        return tables

    async def prompt_schema(self, db: AsyncSession = None, prompt_content = None, with_stats: bool = False, organization: Organization | None = None, top_k: int | None = None) -> str:
        """
        Get the database schema information using TableFormatter.
        Returns a formatted string suitable for prompts.
        
        If prompt_content is provided, also includes relevant resources based on the prompt.
        """
        from app.ai.prompt_formatters import TableFormatter
        # Pass the session to get_schemas
        tables = await self.get_schemas(db=db, with_stats=with_stats, organization=organization, top_k=top_k)
        schema_str = TableFormatter(tables).table_str
        
        resource_context = await self.get_resources(db, prompt_content)
        if resource_context:
            schema_str += f"\n\n{resource_context}"
                
        return schema_str
    
    async def get_resources(self, db: AsyncSession, prompt_content) -> str:
        """
        Get relevant metadata resources associated with this data source based on the prompt.
        Uses ResourceContextBuilder to extract and filter resources.
        """
        # Create a ResourceContextBuilder instance
        context_builder = ResourceContextBuilder(db, prompt_content)
        # Build context for just this data source
        return await context_builder.build_context([self])
    
    def has_membership(self, user_id: str) -> bool:
        """
        Check if a user has explicit membership to this data source.
        This is separate from permissions - just checks membership.
        """
        for membership in self.memberships:
            if (membership.principal_type == "user" and 
                membership.principal_id == user_id):
                return True
        return False
    
    async def has_membership_async(self, user_id: str, db):
        """
        Async version of has_membership that checks memberships from database.
        Use this when memberships might not be loaded.
        """
        from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
        from sqlalchemy import select
        
        stmt = select(DataSourceMembership).where(
            DataSourceMembership.data_source_id == self.id,
            DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
            DataSourceMembership.principal_id == user_id
        )
        result = await db.execute(stmt)
        membership = result.scalar_one_or_none()
        
        return membership is not None