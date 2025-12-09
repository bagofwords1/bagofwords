"""
Demo Data Sources Schema

Defines available demo data sources that can be installed with one click.
Add new demo databases here to make them available in the UI.
"""

from pydantic import BaseModel
from typing import Dict, Any, Optional, List
from datetime import datetime


class DemoDataSourceDefinition(BaseModel):
    """Definition of a demo data source that can be installed."""
    id: str
    name: str
    description: str
    type: str  # e.g., "sqlite", "postgresql"
    config: Dict[str, Any]
    credentials: Dict[str, Any] = {}
    conversation_starters: List[str] = []
    instructions: List[str] = []  # List of instruction texts to create for this data source


class DemoDataSourceListItem(BaseModel):
    """Response schema for listing available demo data sources."""
    id: str
    name: str
    description: str
    type: str
    installed: bool = False
    installed_data_source_id: Optional[str] = None


class DemoDataSourceInstallResponse(BaseModel):
    """Response schema for installing a demo data source."""
    success: bool
    message: str
    data_source_id: Optional[str] = None
    already_installed: bool = False


# Registry of available demo data sources
# Add new demo databases here
DEMO_DATA_SOURCES: Dict[str, DemoDataSourceDefinition] = {
    "chinook": DemoDataSourceDefinition(
        id="chinook",
        name="Chinook Music Store",
        description="Sample music store database with artists, albums, tracks, customers, and invoices. Powered by SQLite.",
        type="sqlite",
        config={
            "database": "demo-datasources/chinook.sqlite",
        },
        credentials={},
        conversation_starters=[
            "What are the top 10 customers by total spend?",
            "Show me revenue breakdown by country",
            "Which genres generate the most sales?",
            "What are the most popular artists by track sales?",
        ],
        instructions=[
            "VIP customers are customers who have spent more than $100 USD in total purchases.",
            "Best sellers are tracks that appeared in the top 5 charts for at least 3 consecutive months.",
            "For 'top X by Y' queries, use a bar chart to display the results."       
            ],
    ),
    "stocks": DemoDataSourceDefinition(
        id="stocks",
        name="Financial Markets Data",
        description="Sample financial database with candlestick data, gold vs bitcoin prices, quotes, bank failures, and country statistics. Powered by DuckDB.",
        type="duckdb",
        config={
            "database": "demo-datasources/stocks.duckdb",
        },
        credentials={},
        conversation_starters=[
            "Compare gold vs bitcoin price correlation over time",
            "Which US states had the most bank failures?",
            "Show me the top 10 countries by GDP per capita",
            "What's the average spread between high and low in the candle data?",
        ],
        instructions=[
            "When showing data from the candle table, use a candlestick chart to display OHLC (open, high, low, close) data.",
            "For 'top X by Y' queries, use a bar chart to display the results.",
            "When comparing assets like gold vs bitcoin, normalize the values to show percentage change from a common start date.",
        ],
    ),
}


def get_demo_data_source(demo_id: str) -> Optional[DemoDataSourceDefinition]:
    """Get a demo data source definition by ID."""
    return DEMO_DATA_SOURCES.get(demo_id)


def list_demo_data_sources() -> list[DemoDataSourceDefinition]:
    """List all available demo data sources."""
    return list(DEMO_DATA_SOURCES.values())
