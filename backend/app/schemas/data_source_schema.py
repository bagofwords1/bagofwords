from pydantic import BaseModel, Field, validator
from typing import Optional, Dict, Any, List
import uuid
from datetime import datetime
import json
from app.schemas.git_repository_schema import GitRepositorySchema


class DataSourceBase(BaseModel):
    name: str = None
    type: str = None  # e.g., "postgresql", "bigquery", "netsuite"
    config: dict = None  # JSON config, will be validated based on the type


class DataSourceSchema(DataSourceBase):
    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime
    context: Optional[str]
    description: Optional[str]
    summary: Optional[str]
    conversation_starters: Optional[list]
    is_active: bool
    config: Dict[str, Any]
    git_repository: Optional[GitRepositorySchema] = None

    @validator('config', pre=True)
    def parse_config(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError('Invalid JSON string for config')
        return value

    @validator('git_repository', pre=True)
    def validate_git_repository(cls, v):
        if v is None:
            return None
        try:
            if isinstance(v, list):
                return v[-1] if v else None
            return v
        except Exception:
            return None

    class Config:
        from_attributes = True
        arbitrary_types_allowed = True


class DataSourceCreate(DataSourceBase):
    credentials: dict  # Will be validated based on the data source type
    generate_summary: bool = False
    generate_conversation_starters: bool = False
    generate_ai_rules: bool = False

    @validator('credentials')
    def validate_credentials(cls, v, values):
        if 'type' not in values:
            raise ValueError('Data source type must be specified')
        
        # Map data source types to their credential schemas
        credential_schemas = {
            'postgresql': PostgreSQLCredentials,
            'snowflake': SnowflakeCredentials,
            'bigquery': BigQueryCredentials,
            'netsuite': NetSuiteCredentials,
            'mysql': SQLCredentials,
            'mariadb': SQLCredentials,
            'MSSQL': SQLCredentials,
            'clickhouse': ClickhouseCredentials,
            'adp': ADPCredentials,
            'salesforce': SalesforceCredentials,
            'service_demo': ServiceDemoCredentials,
            'presto': PrestoCredentials,
            'GCP': GCPCredentials,
            'google_analytics': GoogleAnalyticsCredentials,
            'aws_cost': AWSCostCredentials,
            'aws_athena': AWSAthenaCredentials,
            'vertica': VerticaCredentials
        }
        
        schema = credential_schemas.get(values['type'])
        if not schema:
            raise ValueError(f'Unknown data source type: {values["type"]}')
        
        return schema(**v).dict()


class DataSourceUpdate(DataSourceBase):
    name: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    summary: Optional[str] = None
    config: Optional[dict] = None
    context: Optional[str] = None
    conversation_starters: Optional[list] = None
    credentials: Optional[dict] = None

    class Config:
        from_attributes = True


class DataSourceInDBBase(DataSourceBase):
    id: str
    credentials: Optional[str]

    class Config:
        orm_mode = True


# PostgreSQL
class PostgreSQLCredentials(BaseModel):
    user: str
    password: str

class PostgreSQLConfig(BaseModel):
    host: str
    port: int = Field(5432, ge=1, le=65535)
    database: str

# MySQL/MariaDB/MSSQL - Combined since they share the same structure
class SQLCredentials(BaseModel):
    user: str
    password: str

class SQLConfig(BaseModel):
    host: str
    port: int
    database: str

# Snowflake
class SnowflakeCredentials(BaseModel):
    user: str
    password: str

class SnowflakeConfig(BaseModel):
    account: str
    warehouse: str
    database: str
    schema: str

# BigQuery - credentials_json already contains all auth info
class BigQueryCredentials(BaseModel):
    credentials_json: str

class BigQueryConfig(BaseModel):
    project_id: str
    dataset: str

# NetSuite - all auth related fields should be in credentials
class NetSuiteCredentials(BaseModel):
    consumer_key: str
    consumer_secret: str
    token_id: str
    token_secret: str
    account_id: str  # moved from config since it's part of auth

class NetSuiteConfig(BaseModel):
    pass

# Clickhouse
class ClickhouseCredentials(BaseModel):
    user: str
    password: str

class ClickhouseConfig(BaseModel):
    host: str
    database: str

# ADP - all fields are sensitive
class ADPCredentials(BaseModel):
    client_id: str
    client_secret: str
    username: str
    password: str

class ADPConfig(BaseModel):
    pass

# Salesforce
class SalesforceCredentials(BaseModel):
    username: str
    password: str
    security_token: str

class SalesforceConfig(BaseModel):
    sandbox: bool = False
    domain: str = "login"

# Service Demo
class ServiceDemoCredentials(BaseModel):
    access_key: str
    secret_key: str

class ServiceDemoConfig(BaseModel):
    region: str

# Update the specific config classes to use the new base
class MySQLConfig(SQLConfig):
    port: int = Field(3306, ge=1, le=65535)

class MariadbConfig(SQLConfig):
    port: int = Field(3306, ge=1, le=65535)

class MssqlConfig(SQLConfig):
    port: int = Field(1433, ge=1, le=65535)


# Presto
class PrestoCredentials(BaseModel):
    user: str
    password: str

class PrestoConfig(BaseModel):
    host : str
    port: int = Field(8080, ge=1, le=65535)
    catalog: str
    schema: str
    protocol: str = "http"

# Google Analytics
class GoogleAnalyticsCredentials(BaseModel):
    service_account_file: str
    property_id: str

class GoogleAnalyticsConfig(BaseModel):
    pass

# GCP
class GCPCredentials(BaseModel):
    credentials_json: str
    project_id: str

class GCPConfig(BaseModel):
    pass

# AWS Cost
class AWSCredentials(BaseModel):
    access_key: str
    secret_key: str

class AWSCostCredentials(AWSCredentials):
    pass

class AWSCostConfig(BaseModel):
    region_name: str

# AWS Athena
class AWSAthenaCredentials(BaseModel):
    access_key: str
    secret_key: str
    role_arn: str


class AWSAthenaConfig(BaseModel):
    region: str
    database: str
    workgroup: str = "primary"
    s3_output_location: str  # S3 location where query results are stored
    data_source: str = "AwsDataCatalog"

# Vertica
class VerticaCredentials(BaseModel):
    user: str
    password: str

class VerticaConfig(BaseModel):
    host: str
    port: int = Field(5433, ge=1, le=65535)
    database: str
    schema: str = Field(default="public", description="Schema name")

