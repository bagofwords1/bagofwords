from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
import boto3
import pandas as pd


class ServiceDemoClient(DataSourceClient):
    def __init__(self, access_key: str, secret_key: str):
        self.access_key = access_key
        self.secret_key = secret_key
        self.session = boto3.Session(aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def connect(self):
        """Simulate a connection test."""
        try:
            # Test by listing S3 buckets
            s3 = self.session.client("s3")
            s3.list_buckets()
            return {"success": True, "message": "Connected to Service Demo"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def test_connection(self):
        return self.connect()

    def get_schemas(self):
        """Define static schemas for the demo."""
        schemas = [
            Table(name="EC2", columns=[
                TableColumn(name="InstanceId", dtype="str"),
                TableColumn(name="InstanceType", dtype="str"),
                TableColumn(name="State", dtype="str"),
            ], pks=[TableColumn(name="InstanceId", dtype="str")], fks=[]),
            Table(name="Volumes", columns=[
                TableColumn(name="VolumeId", dtype="str"),
                TableColumn(name="AvailabilityZone", dtype="str"),
                TableColumn(name="SnapshotId", dtype="str"),
                TableColumn(name="Size", dtype="str")
            ], pks=[TableColumn(name="VolumeId", dtype="str")], fks=[]),
            Table(name="S3", columns=[
                TableColumn(name="CreationDate", dtype="str"),
                TableColumn(name="Name", dtype="str"),
            ], pks=[TableColumn(name="Name", dtype="str")], fks=[])
        ]
        return schemas

    def execute_query(self, service_name, operation_name, parameters):
        """Dynamically execute AWS commands and return a DataFrame."""
        try:
            client = self.session.client(service_name)
            operation = getattr(client, operation_name)
            response = operation(**parameters)

            # Normalize AWS response for Pandas DataFrame
            if "Reservations" in response:  # EC2 instances
                data = [instance for res in response["Reservations"] for instance in res["Instances"]]
            elif "Volumes" in response:  # Volumes
                data = response["Volumes"]
            elif "Buckets" in response:  # S3 Buckets
                data = response["Buckets"]
            else:
                data = response
            
            # lower case all column names
            df = pd.json_normalize(data)
            
            return df

        except Exception as e:
            raise RuntimeError(f"Error executing AWS query: {e}")

    def prompt_schema(self):
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str
    
    def run_sql_as_df(self, query):
        pass

    def get_schema(self, table_name):
        pass

    def system_prompt(self):
        """Provide a detailed system prompt for LLM integration."""
        text = """
        ## System Prompt for AWS Service Demo Integration
        This service allows querying AWS data (EC2, Volumes, S3) using boto3.
        Use `execute_query` to dynamically run AWS commands.

        params:
        - service_name: The AWS service name, like "ec2", "s3", etc.
        - operation_name: The AWS operation name, like "describe_instances", "list_buckets", etc.
        - parameters: The parameters for the operation, like {"Filters": [{"Name": "instance-state-name", "Values": ["running"]}]}

        The data source client is wrapping boto3, so the parameters are the similar to boto3.

        Example:
        - EC2 Instances:
          ```python
          df = client.execute_query("ec2", "describe_instances", {"Filters": [{"Name": "instance-state-name", "Values": ["running"]}]})
          ```

        - S3 Buckets:
          ```python
          df = client.execute_query("s3", "list_buckets", {})
          ```

        - Volumes:
          ```python
          df = client.execute_query("ec2", "describe_volumes", {})
          ```
        """
        return text

    @property
    def description(self):
         text = "AWS Service Demo, dynamically execute AWS commands using a wrapper of Boto3."
         return text +  "\n\n" + self.system_prompt()