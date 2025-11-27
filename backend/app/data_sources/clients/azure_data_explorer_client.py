from app.data_sources.clients.base import DataSourceClient

import pandas as pd
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.exceptions import KustoServiceError
from contextlib import contextmanager
from typing import List, Generator
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property


class AzureDataExplorerClient(DataSourceClient):
    """Azure Data Explorer (Kusto) client for querying ADX clusters."""
    
    def __init__(self, cluster_url, database, client_id=None, client_secret=None, tenant_id=None, auth_type="aad_app"):
        """
        Initialize Azure Data Explorer client.
        
        Args:
            cluster_url: The ADX cluster URL (e.g., https://mycluster.region.kusto.windows.net)
            database: The database name
            client_id: Azure AD application (client) ID for authentication
            client_secret: Azure AD application secret
            tenant_id: Azure AD tenant ID
            auth_type: Authentication type - "aad_app" (service principal) or other methods
        """
        self.cluster_url = cluster_url
        self.database = database
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.auth_type = auth_type

    @cached_property
    def kcsb(self):
        """Create Kusto connection string builder."""
        if self.auth_type == "aad_app" and self.client_id and self.client_secret and self.tenant_id:
            # Service principal authentication
            kcsb = KustoConnectionStringBuilder.with_aad_application_key_authentication(
                self.cluster_url,
                self.client_id,
                self.client_secret,
                self.tenant_id
            )
        else:
            # Default to Azure CLI authentication for development
            kcsb = KustoConnectionStringBuilder.with_az_cli_authentication(self.cluster_url)
        
        return kcsb

    @contextmanager
    def connect(self) -> Generator[KustoClient, None, None]:
        """Yield a connection to Azure Data Explorer."""
        client = None
        try:
            client = KustoClient(self.kcsb)
            yield client
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Azure Data Explorer: {e}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def execute_query(self, kql: str) -> pd.DataFrame:
        """
        Execute KQL (Kusto Query Language) statement and return the result as a DataFrame.
        
        Args:
            kql: The KQL query string
            
        Returns:
            pandas.DataFrame with query results
        """
        try:
            with self.connect() as client:
                response = client.execute(self.database, kql)
                
                # Convert Kusto response to pandas DataFrame
                # The primary result table is typically the first one
                if response.primary_results:
                    primary_table = response.primary_results[0]
                    # Convert KustoResultTable to DataFrame manually
                    data = []
                    for row in primary_table:
                        data.append(row.to_dict())
                    df = pd.DataFrame(data)
                    return df
                else:
                    return pd.DataFrame()
                    
        except KustoServiceError as e:
            raise
        except Exception as e:
            raise

    def get_tables(self) -> List[Table]:
        """
        Get all tables and their columns in the specified database.
        Uses .show schema command to retrieve table and column information.
        """
        try:
            with self.connect() as client:
                # Use Kusto management command to get schema information
                kql = f".show database {self.database} schema as json"
                response = client.execute(self.database, kql)
                
                if not response.primary_results:
                    return []
                
                # Parse the schema JSON result - convert manually
                primary_table = response.primary_results[0]
                data = []
                for row in primary_table:
                    data.append(row.to_dict())
                schema_df = pd.DataFrame(data)
                
                tables = {}
                
                if not schema_df.empty and 'DatabaseSchema' in schema_df.columns:
                    import json
                    schema_json = json.loads(schema_df.iloc[0]['DatabaseSchema'])
                    
                    # Parse tables and columns from schema
                    db_schema = schema_json.get('Databases', {})
                    
                    tables_dict = db_schema.get(self.database, {}).get('Tables', {})
                    
                    for table_info in tables_dict.values():
                        table_name = table_info.get('Name')
                        
                        if table_name not in tables:
                            tables[table_name] = Table(
                                name=table_name,
                                columns=[],
                                pks=[],
                                fks=[],
                                metadata_json={"database": self.database}
                            )
                        
                        # Add columns
                        for col_name, col_info in table_info.get('OrderedColumns', []):
                            col_type = col_info.get('Type', 'dynamic')
                            tables[table_name].columns.append(
                                TableColumn(name=col_name, dtype=col_type)
                            )
                
                return list(tables.values())
                
        except Exception as e:
            # Fallback: try to get basic table list
            try:
                with self.connect() as client:
                    kql = ".show tables"
                    response = client.execute(self.database, kql)
                    
                    if response.primary_results:
                        # Convert manually
                        primary_table = response.primary_results[0]
                        data = []
                        for row in primary_table:
                            data.append(row.to_dict())
                        tables_df = pd.DataFrame(data)
                        tables = {}
                        
                        for _, row in tables_df.iterrows():
                            table_name = row.get('TableName', row.get('Name'))
                            if table_name:
                                tables[table_name] = Table(
                                    name=table_name,
                                    columns=[],
                                    pks=[],
                                    fks=[],
                                    metadata_json={"database": self.database}
                                )
                        
                        return list(tables.values())
            except Exception:
                pass
            
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        """Get schemas for all tables in the specified database."""
        return self.get_tables()

    def prompt_schema(self):
        """Return formatted schema information for use in prompts."""
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to Azure Data Explorer and return status information."""
        try:
            with self.connect() as client:
                # Simple query to test connection
                response = client.execute(self.database, "print test='connection successful'")
                
                if response.primary_results:
                    return {
                        "success": True,
                        "message": f"Successfully connected to Azure Data Explorer cluster {self.cluster_url}, database {self.database}"
                    }
                else:
                    return {
                        "success": False,
                        "message": "Connected but received no response"
                    }
                    
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        """Return description of how to use this client for LLM code generation."""
        system_prompt = """
        You can call the execute_query method to run KQL (Kusto Query Language) queries.
        
        The below are examples for how to use the execute_query method. Note that the actual KQL will vary based on the schema.
        Notice only the KQL syntax and instructions on how to use the execute_query method, not the actual KQL queries.

        ```python
        df = client.execute_query("MyTable | take 10")
        ```
        or:
        ```python
        df = client.execute_query("MyTable | where Age > 30 | project Name, Age, City")
        ```
        or with aggregation:
        ```python
        df = client.execute_query("MyTable | summarize Count=count() by City | order by Count desc")
        ```

        Important KQL syntax notes:
        - Use pipe (|) to chain operations
        - Common operators: where, project, summarize, take, order by, join
        - Use single quotes for string literals
        - Date/time functions: ago(), now(), datetime()
        - No semicolons needed at end of queries
        """
        description = f"Azure Data Explorer (Kusto) cluster at {self.cluster_url}, database: {self.database}\n\n"
        description += system_prompt

        return description
