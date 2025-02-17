from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from google.cloud import storage, compute_v1, bigquery, functions_v1, pubsub_v1
from google.oauth2.service_account import Credentials
import pandas as pd


class GCPClient(DataSourceClient):
    def __init__(self, credentials_json: str, project_id: str):
        self.credentials_json = credentials_json
        self.project_id = project_id
        self.credentials = Credentials.from_service_account_file(credentials_json)
        self.storage_client = storage.Client(credentials=self.credentials)
        self.compute_client = compute_v1.InstancesClient(credentials=self.credentials)
        self.disks_client = compute_v1.DisksClient(credentials=self.credentials)
        self.bigquery_client = bigquery.Client(credentials=self.credentials)
        self.functions_client = functions_v1.CloudFunctionsServiceClient(credentials=self.credentials)
        self.pubsub_client = pubsub_v1.PublisherClient(credentials=self.credentials)

    def connect(self):
        """Simulate a connection test."""
        try:
            # Test by listing GCS buckets
            buckets = list(self.storage_client.list_buckets())
            return {"success": True, "message": f"Connected to GCP, found {len(buckets)} buckets."}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def test_connection(self):
        return self.connect()

    def get_schemas(self):
        """Define static schemas for the demo."""
        schemas = [
            Table(name="Compute Instances", columns=[
                TableColumn(name="Name", dtype="str"),
                TableColumn(name="Zone", dtype="str"),
                TableColumn(name="Status", dtype="str"),
                TableColumn(name="MachineType", dtype="str")
            ], pks=[TableColumn(name="Name", dtype="str")], fks=[]),
            Table(name="Disks", columns=[
                TableColumn(name="Name", dtype="str"),
                TableColumn(name="Zone", dtype="str"),
                TableColumn(name="SizeGb", dtype="int"),
                TableColumn(name="Type", dtype="str"),
                TableColumn(name="Status", dtype="str")
            ], pks=[TableColumn(name="Name", dtype="str")], fks=[]),
            Table(name="Storage Buckets", columns=[
                TableColumn(name="Name", dtype="str"),
                TableColumn(name="Location", dtype="str"),
                TableColumn(name="StorageClass", dtype="str"),
                TableColumn(name="TimeCreated", dtype="str")
            ], pks=[TableColumn(name="Name", dtype="str")], fks=[]),
            Table(name="BigQuery Datasets", columns=[
                TableColumn(name="DatasetId", dtype="str"),
                TableColumn(name="ProjectId", dtype="str"),
                TableColumn(name="Location", dtype="str"),
            ], pks=[TableColumn(name="DatasetId", dtype="str")], fks=[]),
            Table(name="Cloud Functions", columns=[
                TableColumn(name="Name", dtype="str"),
                TableColumn(name="Runtime", dtype="str"),
                TableColumn(name="Status", dtype="str"),
                TableColumn(name="EntryPoint", dtype="str")
            ], pks=[TableColumn(name="Name", dtype="str")], fks=[]),
            Table(name="Pub/Sub Topics", columns=[
                TableColumn(name="TopicName", dtype="str")
            ], pks=[TableColumn(name="TopicName", dtype="str")], fks=[])
        ]
        return schemas

    def execute_query(self, service_name, operation_name, parameters):
        """Dynamically execute GCP commands and return a DataFrame."""
        try:

            # Add/override project id parameter 
            parameters = parameters or {}
            parameters['project'] = self.project_id

            client = self.get_client(service_name)
            operation_method = self.get_operation_method(client, service_name, operation_name)

            # Execute the method
            result = operation_method(**parameters)

            # Process the result into a DataFrame
            data = self._process_result(service_name, operation_name, result, parameters)
            df = pd.DataFrame(data)
            return df
        except Exception as e:
            raise RuntimeError(f"Error executing GCP query: {e}")

    def get_client(self, service_name):
        """Retrieve the appropriate client for the given service name."""
        clients = {
            "storage": self.storage_client,
            "compute": self.compute_client,
            "disks": self.disks_client,
            "bigquery": self.bigquery_client,
            "functions": self.functions_client,
            "pubsub": self.pubsub_client
        }
        if service_name not in clients:
            raise ValueError(f"Unsupported service '{service_name}'")
        return clients[service_name]

    def get_operation_method(self, client, service_name, operation_name):
        """Retrieve the appropriate method for the given operation."""
        operations = {
            "storage": {
                "list_buckets": "list_buckets"
            },
            "compute": {
                "list_instances": "list"
            },
            "disks": {
                "list_disks": "list"
            },
            "bigquery": {
                "list_datasets": "list_datasets"
            },
            "functions": {
                "list_functions": "list"
            },
            "pubsub": {
                "list_topics": "list"
            }
        }
        if service_name not in operations or operation_name not in operations[service_name]:
            raise ValueError(f"Unsupported operation '{operation_name}' for service '{service_name}'")
        method_name = operations[service_name][operation_name]
        return getattr(client, method_name)

    def _process_result(self, service_name, operation_name, result, parameters):
        """Process the result based on service and operation."""
        processors = {
            "storage": {
                "list_buckets": self._process_storage_list_buckets
            },
            "compute": {
                "list_instances": self._process_compute_list_instances
            },
            "disks": {
                "list_disks": self._process_compute_list_disks
            },
            "bigquery": {
                "list_datasets": self._process_bigquery_list_datasets
            },
            "functions": {
                "list_functions": self._process_functions_list_functions
            },
            "pubsub": {
                "list_topics": self._process_pubsub_list_topics
            }
        }

        if service_name not in processors or operation_name not in processors[service_name]:
            raise ValueError(f"Unsupported operation '{operation_name}' for service '{service_name}'")

        return processors[service_name][operation_name](result, parameters)

    def _process_storage_list_buckets(self, result, parameters):
        """Process result for listing storage buckets."""
        return [{
            "Name": bucket.name,
            "Location": bucket.location,
            "StorageClass": bucket.storage_class,
            "TimeCreated": bucket.time_created
        } for bucket in result]

    def _process_compute_list_instances(self, result, parameters):
        """Process result for listing compute instances."""
        zone = parameters["zone"]
        return [{
            "Name": instance.name,
            "Zone": zone,
            "Status": instance.status,
            "MachineType": instance.machine_type
        } for instance in result]

    def _process_compute_list_disks(self, result, parameters):
        """Process result for listing compute disks."""
        return [{
            "Name": disk.name,
            "Zone": disk.zone,
            "SizeGb": disk.size_gb,
            "Type": disk.type_,
            "Status": disk.status
        } for disk in result]

    def _process_bigquery_list_datasets(self, result, parameters):
        """Process result for listing BigQuery datasets."""
        data = []
        for dataset in result:
            dataset_info = self.bigquery_client.get_dataset(dataset.dataset_id)
            data.append({
                "DatasetId": dataset.dataset_id,
                "ProjectId": dataset.project,
                "Location": dataset_info.location
            })
        return data

    def _process_functions_list_functions(self, result, parameters):
        """Process result for listing Cloud Functions."""
        return [{
            "Name": function.name,
            "Runtime": function.runtime,
            "Status": function.status,
            "EntryPoint": function.entry_point
        } for function in result]

    def _process_pubsub_list_topics(self, result, parameters):
        """Process result for listing Pub/Sub topics."""
        return [{
            "TopicName": topic.name
        } for topic in result]
            



    def prompt_schema(self):
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str

    def system_prompt(self):
        """Provide a detailed system prompt for LLM integration."""
        text = """
        ## System Prompt for GCP Service Demo Integration
        This service allows querying GCP data (Compute Instances, Disks, Storage Buckets, BigQuery Datasets) using the google-cloud library.
        Use `execute_query` to dynamically run GCP commands.

        params:
        - service_name: The GCP service name, like "storage", "compute", "bigquery".
        - operation_name: The operation name, like "list_buckets", "list_instances", "list_disks", "list_datasets".
        - parameters: The parameters for the operation.

        Example:
        - List Storage Buckets:
          ```python
          df = client.execute_query("storage", "list_buckets", {})
          ```

        - List Compute Instances:
          ```python
          df = client.execute_query("compute", "list_instances", {"project": "my-project", "zone": "us-central1-a"})
          ```

        - List Disks:
          ```python
          df = client.execute_query("disks", "list_disks", {"project": "my-project", "zone": "us-central1-a"})
          ```

        - List BigQuery Datasets:
          ```python
          df = client.execute_query("bigquery", "list_datasets", {})
          ```

        - List Cloud Functions:
          ```python
          df = client.execute_query("functions", "list_functions", {})
          ```

        - List Pub/Sub Topics:
          ```python
          df = client.execute_query("pubsub", "list_topics", {})
          ```
        """
        return text

    @property
    def description(self):
        text = "GCP Service Demo, dynamically execute GCP commands using a wrapper of google-cloud-python libraries."
        return text + "\n\n" + self.system_prompt()

    def get_schema(self, table_id: str):
        """This method is now obsolete"""
        raise NotImplementedError("get_schema() is obsolete in this class")

    def get_tables(self):
        """This method is now obsolete"""
        raise NotImplementedError("get_tables() is obsolete in this class")
