from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from simple_salesforce import Salesforce
import pandas as pd
from typing import List, Generator
from functools import cached_property
from contextlib import contextmanager

class SalesforceClient(DataSourceClient):
    def __init__(self, username: str, password: str, security_token: str, domain: str, sandbox: bool=True):
        self.username = username
        self.password = password
        self.security_token = security_token
        self.domain = domain

    @cached_property
    def sf(self):
        return Salesforce(username=self.username, password=self.password, security_token=self.security_token)

    @contextmanager
    def connect(self) -> Generator[Salesforce, None, None]:
        """Yield a connection to Salesforce."""
        try:
            yield self.sf
        except Exception as e:
            raise RuntimeError(f"Error while connecting to Salesforce: {e}")

    def test_connection(self):
        """Test the Salesforce connection."""
        try:
            with self.connect() as sf:
                sf.describe()
                return {"success": True, "message": "Connected to Salesforce"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_schemas(self) -> List[Table]:
        """Get schemas for common Salesforce objects."""
        object_names = ["Account", "Contact", "Opportunity", "Lead", "Case"]
        schemas = [self.get_schema(object_name) for object_name in object_names]
        return schemas

    def execute_query(self, query: str) -> pd.DataFrame:
        """Execute a SOQL query and return results as a DataFrame."""
        try:
            with self.connect() as sf:
                results = sf.query_all(query)
                df = pd.DataFrame(results['records'])
                if not df.empty:
                    df = df.drop('attributes', axis=1)
                return df
        except Exception as e:
            raise RuntimeError(f"Error executing Salesforce query: {e}")

    def prompt_schema(self):
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str
    
    def get_schema(self, object_name: str) -> Table:
        """Get schema for a specific Salesforce object."""
        with self.connect() as sf:
            describe = sf.__getattr__(object_name).describe()
            columns = [TableColumn(name=field['name'], dtype=field['type']) for field in describe['fields']]
            return Table(name=object_name, columns=columns, pks=[TableColumn(name="Id", dtype="str")], fks=[])

    def system_prompt(self):
        """Provide a detailed system prompt for LLM integration."""
        text = """
        ## System Prompt for Salesforce Integration
        This service allows querying Salesforce data using SOQL (Salesforce Object Query Language).
        Use `execute_query` to run SOQL queries.

        Example:
        ```python
        df = client.execute_query("SELECT Id, Name, Type, Industry FROM Account LIMIT 10")
        df_lead = client.execute_query("SELECT Id, FirstName, LastName, Company, Status FROM Lead LIMIT 10")
        df_case = client.execute_query("SELECT Id, CaseNumber, Status, Priority, Subject FROM Case LIMIT 10")

        SOQL is similar to SQL but has some differences:
        1. FROM clause comes immediately after SELECT
        2. Supports relationship queries using dot notation
        3. No table aliases or joins (use relationship queries instead)
        4. LIMIT clause is required for performance reasons

        Common objects:
        - Account
        - Contact
        - Opportunity
        - Lead
        - Case

        For more details on SOQL, refer to the Salesforce documentation.
        """
        return text

    @property
    def description(self):
         text = "Salesforce Client, execute SOQL queries to retrieve Salesforce data."
         return text + "\n\n" + self.system_prompt()
