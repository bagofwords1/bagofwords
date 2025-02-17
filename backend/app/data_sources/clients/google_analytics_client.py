from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
from google.api_core.exceptions import GoogleAPICallError, InvalidArgument
from typing import List, Generator, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from contextlib import contextmanager
import pandas as pd
import os

from app.data_sources.clients.base import DataSourceClient


class GoogleAnalyticsClient(DataSourceClient):
    def __init__(self, service_account_file: Optional[str] = None, property_id: str = None):
        super().__init__()
        self.service_account_file = service_account_file or os.getenv('GA_SERVICE_ACCOUNT_FILE')
        self.property_id = property_id or os.getenv('GA_PROPERTY_ID')

    @cached_property
    def client(self):
        return BetaAnalyticsDataClient.from_service_account_file(self.service_account_file)

    @contextmanager
    def connect(self) -> Generator[BetaAnalyticsDataClient, None, None]:
        """Yield a connection to Google Analytics."""
        try:
            yield self.client
        except Exception as e:
            raise RuntimeError(f"Connection error: {e}")

    def execute_query(self, query: str = None, start_date: Optional[str] = None, end_date: Optional[str] = None, dimensions: Optional[List[str]] = None, metrics: Optional[List[str]] = None) -> pd.DataFrame:
        """
        Execute a Google Analytics query and return the result as a DataFrame.
        
        Args:
            start_date (str): The start date for the query in YYYY-MM-DD format.
            end_date (str): The end date for the query in YYYY-MM-DD format.
            dimensions (List[str]): The list of dimension names to include in the report.
            metrics (List[str]): The list of metric names to include in the report.
        
        Returns:
            pd.DataFrame: A DataFrame containing the result of the query.
        """
        try:
            with self.connect() as client:
                return self.get_report(client, self.property_id, start_date, end_date, dimensions, metrics)
        except GoogleAPICallError as e:
            raise RuntimeError(f"API call error: {e}")
        except InvalidArgument as e:
            raise ValueError(f"Invalid argument error: {e}")
        except Exception as e:
            raise RuntimeError(f"Error executing query: {e}")

    def get_report(
        self,
        client: BetaAnalyticsDataClient,
        property_id: str,
        start_date: str,
        end_date: str,
        dimensions: Optional[List[str]] = None,
        metrics: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        Generates a report from Google Analytics 4 using the provided dimensions and metrics.

        Args:
            client (BetaAnalyticsDataClient): The Google Analytics BetaAnalyticsDataClient.
            property_id (str): The property ID for the Google Analytics account.
            start_date (str): The start date for the report in 'YYYY-MM-DD' format.
            end_date (str): The end date for the report in 'YYYY-MM-DD' format.
            dimensions (Optional[List[str]]): A list of dimension names. Defaults to ["date", "pagePath"].
            metrics (Optional[List[str]]): A list of metric names. Defaults to ["activeUsers", "sessions", "screenPageViews", "averageSessionDuration", "bounceRate", "engagementRate", "newUsers", "userEngagementDuration", "conversions", "totalRevenue", "eventCount", "pageViewsPerSession"].

        Returns:
            pd.DataFrame: A DataFrame containing the report data.

        Raises:
            RuntimeError: If an API call error or an unexpected error occurs.
            ValueError: If an invalid argument is passed.
        """
        # Set default dimensions and metrics if not provided
        if dimensions is None:
            dimensions = ["date", "pagePath"]
        if metrics is None:
            metrics = [
                "activeUsers", "sessions", "screenPageViews", "averageSessionDuration", "bounceRate",
                "engagementRate", "newUsers", "userEngagementDuration", "conversions", "totalRevenue",
                "eventCount", "pageViewsPerSession"
            ]

        # Create dimension and metric objects
        dimension_objects = [Dimension(name=dim) for dim in dimensions]
        metric_objects = [Metric(name=met) for met in metrics]

        # Create a request to get the report
        request = RunReportRequest(
            property=f"properties/{property_id}",
            dimensions=dimension_objects,
            metrics=metric_objects,
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        )

        # Make the API call with error handling
        try:
            response = client.run_report(request)
            data = []

            if not response.rows:
                return pd.DataFrame(columns=[*dimensions, *metrics])

            for row in response.rows:
                row_data = {dim: row.dimension_values[i].value for i, dim in enumerate(dimensions)}
                row_data.update({met: row.metric_values[i].value for i, met in enumerate(metrics)})
                data.append(row_data)
            
            return pd.DataFrame(data)

        except GoogleAPICallError as e:
            raise RuntimeError(f"API call error: {e}")
        except InvalidArgument as e:
            raise ValueError(f"Invalid argument error: {e}")
        except Exception as e:
            raise RuntimeError(f"An unexpected error occurred: {e}")

    def get_schemas(self) -> List[Table]:
        """Get schemas for all reports in Google Analytics."""
        schemas = [
            Table(name="Sessions Report", columns=[
                TableColumn(name="date", dtype="string"),
                TableColumn(name="sessionId", dtype="string"),
                TableColumn(name="activeUsers", dtype="integer"),
                TableColumn(name="sessions", dtype="integer"),
                TableColumn(name="screenPageViews", dtype="integer"),
                TableColumn(name="averageSessionDuration", dtype="float")
            ], pks=[TableColumn(name="sessionId", dtype="string")], fks=[]),
            Table(name="Traffic Source Report", columns=[
                TableColumn(name="date", dtype="string"),
                TableColumn(name="source", dtype="string"),
                TableColumn(name="medium", dtype="string"),
                TableColumn(name="campaign", dtype="string"),
                TableColumn(name="sessions", dtype="integer"),
                TableColumn(name="activeUsers", dtype="integer")
            ], pks=[TableColumn(name="source", dtype="string")], fks=[]),
            Table(name="User Demographics Report", columns=[
                TableColumn(name="date", dtype="string"),
                TableColumn(name="country", dtype="string"),
                TableColumn(name="region", dtype="string"),
                TableColumn(name="city", dtype="string"),
                TableColumn(name="activeUsers", dtype="integer"),
                TableColumn(name="sessions", dtype="integer")
            ], pks=[TableColumn(name="country", dtype="string")], fks=[]),
            Table(name="Device Category Report", columns=[
                TableColumn(name="date", dtype="string"),
                TableColumn(name="deviceCategory", dtype="string"),
                TableColumn(name="activeUsers", dtype="integer"),
                TableColumn(name="sessions", dtype="integer"),
                TableColumn(name="screenPageViews", dtype="integer")
            ], pks=[TableColumn(name="deviceCategory", dtype="string")], fks=[]),
            Table(name="Events Report", columns=[
                TableColumn(name="date", dtype="string"),
                TableColumn(name="eventName", dtype="string"),
                TableColumn(name="eventCount", dtype="integer"),
                TableColumn(name="engagementRate", dtype="float")
            ], pks=[TableColumn(name="eventName", dtype="string")], fks=[]),
            Table(name="Bounce and Engagement Report", columns=[
                TableColumn(name="date", dtype="string"),
                TableColumn(name="pagePath", dtype="string"),
                TableColumn(name="bounceRate", dtype="float"),
                TableColumn(name="engagementRate", dtype="float"),
                TableColumn(name="sessions", dtype="integer")
            ], pks=[TableColumn(name="pagePath", dtype="string")], fks=[]),
        ]
        return schemas

    def get_schema(self, table_name: str) -> Table:
        """Get schema for a specific report type."""
        schemas = self.get_schemas()
        for schema in schemas:
            if schema.name == table_name:
                return schema
        raise ValueError("Unknown table name.")

    def prompt_schema(self) -> str:
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self) -> dict:
        """Test connection to Google Analytics and return status information."""
        try:
            with self.connect() as client:
                # Attempt to run a simple query to test the connection
                request = RunReportRequest(
                    property=f"properties/{self.property_id}",
                    dimensions=[Dimension(name="date")],
                    metrics=[Metric(name="activeUsers")],
                    date_ranges=[DateRange(start_date="2024-12-01", end_date="2024-12-01")],
                )
                client.run_report(request)
                return {
                    "success": True,
                    "message": "Successfully connected to Google Analytics"
                }
        except GoogleAPICallError as e:
            return {
                "success": False,
                "message": f"API call error: {e}"
            }
        except InvalidArgument as e:
            return {
                "success": False,
                "message": f"Invalid argument error: {e}"
            }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self) -> str:
        system_prompt = """
        You can call the execute_query method to run Google Analytics queries.
        
        The below are examples for how to use the execute_query method. Note that the actual metrics and dimensions will vary based on your Google Analytics property.
        Notice only the instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        report = client.execute_query(start_date="2024-12-01", end_date="2024-12-05", dimensions=["date", "pagePath"], metrics=["activeUsers", "sessions"])
        ```
        
        The result will be a pandas DataFrame containing metrics and dimensions.
        Make sure to:
        - Use valid metrics and dimensions based on your specific Google Analytics property.
        - Verify that start_date and end_date are in the correct format (YYYY-MM-DD).
        - Handle potential exceptions, such as invalid arguments or API errors, to avoid bad calls.
        
        The execute_query method supports all metrics available in Google Analytics, including but not limited to:
        - activeUsers
        - sessions
        - screenPageViews
        - averageSessionDuration
        - bounceRate
        - engagementRate
        - newUsers
        - userEngagementDuration
        - conversions
        - totalRevenue
        - eventCount
        - pageViewsPerSession
        Make sure to validate the availability of the metrics based on your specific Google Analytics property.
        """
        return f"Google Analytics property at {self.property_id}\n\n" + system_prompt
