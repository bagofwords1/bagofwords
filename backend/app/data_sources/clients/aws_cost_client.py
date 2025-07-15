from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
import boto3
import pandas as pd
from contextlib import contextmanager


class AwsCostClient(DataSourceClient):
    def __init__(self, access_key: str, secret_key: str, region_name: str = "us-east-1"):
        self.access_key = access_key
        self.secret_key = secret_key
        self.region_name = region_name
        self.session = boto3.Session(aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    @contextmanager
    def connect(self):
        """Simulate a connection test."""
        try:
            # Test by creating an AWS Cost Explorer client
            client = self.session.client("ce")
            yield client
        except Exception as e:
            yield None
            raise RuntimeError(f"Error connecting to AWS Cost Explorer: {e}")

    def test_connection(self):
        with self.connect() as connection:
            if connection:
                return {"success": True, "message": "Connected to AWS Cost Explorer"}
            else:
                return {"success": False, "message": "Failed to connect to AWS Cost Explorer"}

    def get_schemas(self):
        """Define schemas for AWS Cost Explorer."""
        schemas = [
            {
                "name": "CostAndUsage",
                "columns": [
                    {"name": "TimePeriod.Start", "dtype": "str"},
                    {"name": "TimePeriod.End", "dtype": "str"},
                    {"name": "BlendedCost.Amount", "dtype": "str"},
                    {"name": "BlendedCost.Unit", "dtype": "str"},
                    {"name": "UnblendedCost.Amount", "dtype": "str"},
                    {"name": "UnblendedCost.Unit", "dtype": "str"},
                    {"name": "AmortizedCost.Amount", "dtype": "str"},
                    {"name": "AmortizedCost.Unit", "dtype": "str"},
                    {"name": "NetUnblendedCost.Amount", "dtype": "str"},
                    {"name": "NetUnblendedCost.Unit", "dtype": "str"},
                    {"name": "NetAmortizedCost.Amount", "dtype": "str"},
                    {"name": "NetAmortizedCost.Unit", "dtype": "str"},
                    {"name": "NetBlendedCost.Amount", "dtype": "str"},
                    {"name": "NetBlendedCost.Unit", "dtype": "str"},
                    {"name": "UsageQuantity.Amount", "dtype": "str"},
                    {"name": "UsageQuantity.Unit", "dtype": "str"},
                    {"name": "SERVICE", "dtype": "str"},
                    {"name": "Region", "dtype": "str"},
                    {"name": "InstanceType", "dtype": "str"},
                    {"name": "LinkedAccountName", "dtype": "str"},
                    {"name": "LinkedAccountId", "dtype": "str"},
                    {"name": "UsageType", "dtype": "str"},
                    {"name": "Operation", "dtype": "str"},
                    {"name": "AvailabilityZone", "dtype": "str"},
                    {"name": "Tenancy", "dtype": "str"},
                    {"name": "Platform", "dtype": "str"},
                    {"name": "SubLocation", "dtype": "str"},
                    {"name": "SubscriptionId", "dtype": "str"},
                    {"name": "DatabaseEngine", "dtype": "str"},
                    {"name": "CacheEngine", "dtype": "str"},
                    {"name": "RightsizingType", "dtype": "str"},
                    {"name": "SavingsPlansType", "dtype": "str"},
                    {"name": "SavingsPlanArn", "dtype": "str"},
                    {"name": "PaymentOption", "dtype": "str"}
                ],
                "pks": [{"name": "TimePeriod.Start", "dtype": "str"}],
                "fks": []
            },
            {
                "name": "GroupingOptions",
                "columns": [
                    {"name": "GroupBy.Type", "dtype": "str"},
                    {"name": "GroupBy.Key", "dtype": "str"}
                ],
                "pks": [],
                "fks": []
            },
            {
                "name": "FilterOptions",
                "columns": [
                    {"name": "Dimensions.Key", "dtype": "str"},
                    {"name": "Dimensions.Values", "dtype": "str"},
                    {"name": "Tags.Key", "dtype": "str"},
                    {"name": "Tags.Values", "dtype": "str"},
                    {"name": "CostCategories.Key", "dtype": "str"},
                    {"name": "CostCategories.Values", "dtype": "str"}
                ],
                "pks": [],
                "fks": []
            }
        ]
        return schemas
    
    def output_schema(self):
        """
        {
            "ResultsByTime": [
                {
                "TimePeriod": {
                    "Start": "string",  // Start date in "YYYY-MM-DD" format
                    "End": "string"    // End date in "YYYY-MM-DD" format
                },
                "Total": {
                    "key": {
                    "Amount": "string", // Cost amount (e.g., "123.45")
                    "Unit": "string"    // Unit of the amount (e.g., "USD")
                    }
                },
                "Groups": [
                    {
                    "Keys": [
                        "string"  // Group keys, e.g., service or tag values
                    ],
                    "Metrics": {
                        "key": {
                        "Amount": "string", // Metric value
                        "Unit": "string"    // Metric unit
                        }
                    }
                    }
                ],
                "Estimated": boolean  // Indicates if the data is estimated
                }
            ],
            "GroupDefinitions": [
                {
                "Type": "string",     // Group type (e.g., "DIMENSION", "TAG")
                "Key": "string"       // Group key (e.g., "SERVICE", "USAGE_TYPE")
                }
            ],
            "NextPageToken": "string" // Token for retrieving the next set of results
        }
        """

    def execute_query(self, operation_name, parameters):
        """Dynamically execute AWS Cost Explorer commands and return a DataFrame."""
        with self.connect() as client:
            if client is None:
                raise RuntimeError("Failed to connect to AWS Cost Explorer")
            try:
                operation = getattr(client, operation_name)
                response = operation(**parameters)
                # Flatten the response structure
                if "ResultsByTime" in response:
                    data = []
                    group_by_keys = [group["Key"] for group in parameters.get("GroupBy", [])]
                    metric_keys = parameters.get("Metrics", [])
                    
                    for result in response["ResultsByTime"]:
                        time_period_start = result.get("TimePeriod", {}).get("Start", None)
                        time_period_end = result.get("TimePeriod", {}).get("End", None)
                        
                        if "Groups" in result:
                            for group in result["Groups"]:
                                row = {}
                                # Add time period columns
                                row["TimePeriod.Start"] = time_period_start
                                row["TimePeriod.End"] = time_period_end
                                
                                # Add group-by dimension columns
                                for i, dimension in enumerate(group.get("Keys", [])):
                                    if i < len(group_by_keys):
                                        row[group_by_keys[i]] = dimension
                                
                                # Add metric columns
                                for metric_name, metric_data in group.get("Metrics", {}).items():
                                    row[f"{metric_name}.Amount"] = metric_data.get("Amount")
                                    row[f"{metric_name}.Unit"] = metric_data.get("Unit")
                                
                                data.append(row)
                        
                        if "Total" in result:
                            total_row = {}
                            total_row["TimePeriod.Start"] = time_period_start
                            total_row["TimePeriod.End"] = time_period_end
                            for metric_name, metric_data in result["Total"].items():
                                total_row[f"{metric_name}.Amount"] = metric_data.get("Amount")
                                total_row[f"{metric_name}.Unit"] = metric_data.get("Unit")
                            data.append(total_row)
                    
                    # Convert to DataFrame
                    df = pd.DataFrame(data)

                    return df
                else:
                    raise RuntimeError("Unexpected response structure")
            
            except Exception as e:
                raise RuntimeError(f"Error executing AWS Cost Explorer query: {e}")

    def prompt_schema(self):
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str
    

    def get_schema(self, table_id: str) -> Table:
        """Placeholder implementation for the abstract method."""
        raise NotImplementedError(
            "get_schema() is not implemented in AwsCostClient.")

    def system_prompt(self):
        """Provide a detailed system prompt for LLM integration."""
        text = """
        ## System Prompt for AWS Cost Explorer Integration
        This service allows querying AWS cost and usage data using boto3.
        Use `execute_query` to dynamically run AWS Cost Explorer commands.


        Data Model Guidelines:
        - When designing the data model, keep in mind that this is an API that returns a JSON object
        that already has some columns, so specify them also in the data model.

        Example Usage:
        - Get Cost and Usage:
        ```python
        # Example 1: Retrieve daily unblended cost for a specific time period
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-05"},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"]
        })

        # Example 2: Group costs by service
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-05"},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]
        })

        # Example 3: Filter costs by a specific linked account
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-05"},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "Filter": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["123456789012"]}}
        })

        # Example 4: Retrieve usage quantity grouped by resource type
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-05"},
            "Granularity": "DAILY",
            "Metrics": ["UsageQuantity"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
        })

        # Example 6: Retrieve total blended cost for a monthly time period
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-31"},
            "Granularity": "MONTHLY",
            "Metrics": ["BlendedCost"]
        })

        # Example 7: Filter costs by tags (e.g., Environment=Production)
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-05"},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "Filter": {"Tags": {"Key": "Environment", "Values": ["Production"]}}
        })

        # Example 8: Retrieve cost and usage grouped by operation type (e.g., RunInstances)
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-05"},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "OPERATION"}]
        })

        # Example 9: Filter costs by specific service (e.g., Amazon EC2)
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-05"},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "Filter": {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon EC2"]}}
        })

        # Example 11: Filter costs by custom time ranges (e.g., last 7 days)
        df = client.execute_query("get_cost_and_usage", {
            "TimePeriod": {"Start": "2024-11-28", "End": "2024-12-05"},
            "Granularity": "DAILY",
            "Metrics": ["BlendedCost"]
        })

        
        Output guidelines:
        - Avoid renaming of columns, unless it is explicitly mentioned
        - Key columns are in the context of: service, date, and amount
        - Drop rows that have NaN values in service

        - The output of the flattened df, may include the following columns: 
          - TimePeriod.Start
          - TimePeriod.End
          - BlendedCost.Amount
          - BlendedCost.Unit
          - UnblendedCost.Amount
          - UnblendedCost.Unit
          - AmortizedCost.Amount
          - AmortizedCost.Unit
          - NetUnblendedCost.Amount
          - NetUnblendedCost.Unit
          - NetAmortizedCost.Amount
          - NetAmortizedCost.Unit
          - NetBlendedCost.Amount
          - NetBlendedCost.Unit
          - UsageQuantity.Amount
          - UsageQuantity.Unit
          - SERVICE
          - Region
          - InstanceType
          - LinkedAccountName
          - LinkedAccountId
          - UsageType
          - Operation
          - AvailabilityZone
          - Tenancy
          - Platform
          - SubLocation
          - SubscriptionId
          - DatabaseEngine
          - CacheEngine
          - RightsizingType
          - SavingsPlansType
          - SavingsPlanArn
          - PaymentOption


        ```

        The data source client wraps boto3's Cost Explorer, so you can easily adapt the examples to match your use case.
        """
        return text



    @property
    def description(self):
         text = "AWS Cost Explorer Client, dynamically execute AWS Cost Explorer commands using a wrapper of Boto3."
         return text +  "\n\n" + self.system_prompt()




