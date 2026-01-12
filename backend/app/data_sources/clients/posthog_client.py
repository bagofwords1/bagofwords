from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
import requests
import pandas as pd
from contextlib import contextmanager
from typing import Optional


# Predefined HogQL table schemas based on PostHog's data model
POSTHOG_SCHEMAS = [
    {
        "name": "events",
        "columns": [
            {"name": "uuid", "dtype": "str"},
            {"name": "event", "dtype": "str"},
            {"name": "timestamp", "dtype": "datetime"},
            {"name": "distinct_id", "dtype": "str"},
            {"name": "properties", "dtype": "json"},
            {"name": "elements_chain", "dtype": "str"},
            {"name": "created_at", "dtype": "datetime"},
            {"name": "$session_id", "dtype": "str"},
            {"name": "$window_id", "dtype": "str"},
            {"name": "person_id", "dtype": "str"},
            {"name": "person.properties", "dtype": "json"},
        ],
        "pks": [{"name": "uuid", "dtype": "str"}],
        "fks": []
    },
    {
        "name": "persons",
        "columns": [
            {"name": "id", "dtype": "str"},
            {"name": "created_at", "dtype": "datetime"},
            {"name": "properties", "dtype": "json"},
            {"name": "is_identified", "dtype": "bool"},
        ],
        "pks": [{"name": "id", "dtype": "str"}],
        "fks": []
    },
    {
        "name": "sessions",
        "columns": [
            {"name": "session_id", "dtype": "str"},
            {"name": "distinct_id", "dtype": "str"},
            {"name": "$start_timestamp", "dtype": "datetime"},
            {"name": "$end_timestamp", "dtype": "datetime"},
            {"name": "$entry_current_url", "dtype": "str"},
            {"name": "$exit_current_url", "dtype": "str"},
            {"name": "$entry_pathname", "dtype": "str"},
            {"name": "$exit_pathname", "dtype": "str"},
            {"name": "$pageview_count", "dtype": "int"},
            {"name": "$autocapture_count", "dtype": "int"},
            {"name": "$session_duration", "dtype": "int"},
            {"name": "$is_bounce", "dtype": "bool"},
            {"name": "$channel_type", "dtype": "str"},
            {"name": "$entry_utm_source", "dtype": "str"},
            {"name": "$entry_utm_campaign", "dtype": "str"},
            {"name": "$entry_utm_medium", "dtype": "str"},
            {"name": "$entry_referring_domain", "dtype": "str"},
        ],
        "pks": [{"name": "session_id", "dtype": "str"}],
        "fks": []
    },
    {
        "name": "person_distinct_ids",
        "columns": [
            {"name": "distinct_id", "dtype": "str"},
            {"name": "person_id", "dtype": "str"},
            {"name": "team_id", "dtype": "int"},
        ],
        "pks": [{"name": "distinct_id", "dtype": "str"}],
        "fks": [
            {
                "column": {"name": "person_id", "dtype": "str"},
                "references_name": "persons",
                "references_column": {"name": "id", "dtype": "str"}
            }
        ]
    },
    {
        "name": "groups",
        "columns": [
            {"name": "group_type_index", "dtype": "int"},
            {"name": "group_key", "dtype": "str"},
            {"name": "created_at", "dtype": "datetime"},
            {"name": "properties", "dtype": "json"},
        ],
        "pks": [{"name": "group_key", "dtype": "str"}],
        "fks": []
    },
    {
        "name": "cohort_people",
        "columns": [
            {"name": "person_id", "dtype": "str"},
            {"name": "cohort_id", "dtype": "int"},
        ],
        "pks": [],
        "fks": [
            {
                "column": {"name": "person_id", "dtype": "str"},
                "references_name": "persons",
                "references_column": {"name": "id", "dtype": "str"}
            }
        ]
    },
    {
        "name": "session_replay_events",
        "columns": [
            {"name": "session_id", "dtype": "str"},
            {"name": "team_id", "dtype": "int"},
            {"name": "distinct_id", "dtype": "str"},
            {"name": "min_first_timestamp", "dtype": "datetime"},
            {"name": "max_last_timestamp", "dtype": "datetime"},
            {"name": "click_count", "dtype": "int"},
            {"name": "keypress_count", "dtype": "int"},
            {"name": "console_log_count", "dtype": "int"},
            {"name": "console_warn_count", "dtype": "int"},
            {"name": "console_error_count", "dtype": "int"},
        ],
        "pks": [{"name": "session_id", "dtype": "str"}],
        "fks": []
    },
]


class PostHogClient(DataSourceClient):
    """PostHog analytics data source client using HogQL queries."""

    def __init__(self, api_key: str, host: str = "https://us.posthog.com", project_id: str = None):
        self.api_key = api_key
        self.host = host.rstrip('/')
        self.project_id = project_id
        self.base_url = f"{self.host}/api/projects/{project_id}"
        self._session = None

    @contextmanager
    def connect(self):
        """Create a requests session for API calls."""
        try:
            session = requests.Session()
            session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            })
            self._session = session
            yield session
        except Exception as e:
            raise RuntimeError(f"Error connecting to PostHog: {e}")
        finally:
            if self._session:
                self._session.close()
                self._session = None

    def test_connection(self) -> dict:
        """Test connection by fetching project info."""
        with self.connect() as session:
            try:
                # Try to get project info to validate credentials
                response = session.get(
                    f"{self.host}/api/projects/{self.project_id}/",
                    timeout=30
                )
                if response.status_code == 200:
                    project_data = response.json()
                    return {
                        "success": True,
                        "message": f"Connected to PostHog project: {project_data.get('name', self.project_id)}"
                    }
                elif response.status_code == 401:
                    return {
                        "success": False,
                        "message": "Invalid API key. Please check your PostHog Personal API Key."
                    }
                elif response.status_code == 404:
                    return {
                        "success": False,
                        "message": f"Project {self.project_id} not found. Please check your Project ID."
                    }
                else:
                    return {
                        "success": False,
                        "message": f"PostHog API error: {response.status_code} - {response.text}"
                    }
            except requests.exceptions.RequestException as e:
                return {
                    "success": False,
                    "message": f"Connection error: {str(e)}"
                }

    def get_schemas(self) -> list:
        """Return predefined HogQL table schemas."""
        return POSTHOG_SCHEMAS

    def get_schema(self, table_name: str) -> Table:
        """Get schema for a specific table."""
        for schema in POSTHOG_SCHEMAS:
            if schema["name"] == table_name:
                columns = [
                    TableColumn(name=col["name"], dtype=col["dtype"])
                    for col in schema["columns"]
                ]
                pks = [
                    TableColumn(name=pk["name"], dtype=pk["dtype"])
                    for pk in schema.get("pks", [])
                ]
                return Table(name=table_name, columns=columns, pks=pks, fks=[])
        raise ValueError(f"Table {table_name} not found in PostHog schema")

    def execute_query(self, query: str, limit: Optional[int] = None) -> pd.DataFrame:
        """Execute a HogQL query and return results as DataFrame.

        Args:
            query: HogQL query string (e.g., "SELECT * FROM events LIMIT 100")
            limit: Optional limit override (applied if query doesn't have LIMIT)

        Returns:
            pandas DataFrame with query results
        """
        with self.connect() as session:
            try:
                # Build the query payload
                payload = {
                    "query": {
                        "kind": "HogQLQuery",
                        "query": query
                    }
                }

                response = session.post(
                    f"{self.base_url}/query/",
                    json=payload,
                    timeout=120  # HogQL queries can take time
                )

                if response.status_code != 200:
                    error_detail = response.text
                    try:
                        error_json = response.json()
                        error_detail = error_json.get("detail", error_json.get("error", response.text))
                    except Exception:
                        pass
                    raise RuntimeError(f"PostHog query failed ({response.status_code}): {error_detail}")

                result = response.json()

                # Handle HogQL response format
                if "results" in result and "columns" in result:
                    df = pd.DataFrame(result["results"], columns=result["columns"])
                    return df
                elif "error" in result:
                    raise RuntimeError(f"HogQL error: {result['error']}")
                else:
                    raise RuntimeError(f"Unexpected response format: {result}")

            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"Error executing PostHog query: {e}")

    def prompt_schema(self) -> str:
        """Generate schema string for LLM prompts."""
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self) -> str:
        """Provide a detailed system prompt for LLM integration."""
        return """
## PostHog Analytics Integration

This connector allows querying PostHog product analytics data using HogQL (PostHog's SQL dialect over ClickHouse).

### Available Tables:

1. **events** - All tracked events
   - Key columns: uuid, event, timestamp, distinct_id, properties, person_id, $session_id
   - Properties accessed via: properties.$property_name
   - Person properties via: person.properties.$property_name

2. **persons** - User/person entities
   - Key columns: id, created_at, properties, is_identified

3. **sessions** - Session aggregates (auto-captured)
   - Key columns: session_id, distinct_id, $start_timestamp, $end_timestamp
   - Metrics: $pageview_count, $session_duration, $is_bounce, $channel_type
   - UTM data: $entry_utm_source, $entry_utm_campaign, $entry_utm_medium

4. **person_distinct_ids** - Maps distinct_id to person_id

5. **groups** - Organization/company grouping entities

6. **session_replay_events** - Session recording metadata

### Query Examples:

```sql
-- Daily active users last 7 days
SELECT
    toDate(timestamp) as date,
    count(DISTINCT distinct_id) as dau
FROM events
WHERE timestamp >= now() - INTERVAL 7 DAY
GROUP BY date
ORDER BY date

-- Top events by count
SELECT event, count() as count
FROM events
WHERE timestamp >= now() - INTERVAL 30 DAY
GROUP BY event
ORDER BY count DESC
LIMIT 10

-- Session metrics by channel
SELECT
    $channel_type,
    count() as sessions,
    avg($session_duration) as avg_duration_seconds,
    countIf($is_bounce = true) * 100.0 / count() as bounce_rate_pct
FROM sessions
WHERE $start_timestamp >= now() - INTERVAL 7 DAY
GROUP BY $channel_type
ORDER BY sessions DESC

-- Page views by URL
SELECT
    properties.$current_url as url,
    count() as views
FROM events
WHERE event = '$pageview'
  AND timestamp >= now() - INTERVAL 7 DAY
GROUP BY url
ORDER BY views DESC
LIMIT 20

-- User acquisition by initial referrer
SELECT
    person.properties.$initial_referrer as referrer,
    count(DISTINCT distinct_id) as users
FROM events
WHERE timestamp >= now() - INTERVAL 30 DAY
GROUP BY referrer
ORDER BY users DESC
LIMIT 10

-- Funnel: signup to purchase
SELECT
    count(DISTINCT if(event = 'signup', distinct_id, NULL)) as signups,
    count(DISTINCT if(event = 'purchase', distinct_id, NULL)) as purchases
FROM events
WHERE timestamp >= now() - INTERVAL 30 DAY
```

### Property Access Patterns:
- Event properties: `properties.$property_name` or `properties.custom_property`
- Person properties: `person.properties.$initial_browser`, `person.properties.email`
- Session fields use $ prefix: `$session_duration`, `$is_bounce`, `$channel_type`

### Important Notes:
- Timestamps use ClickHouse functions: `now()`, `toDate()`, `INTERVAL N DAY/HOUR/MONTH`
- Default query limit is 100 rows; use `LIMIT` clause for more
- Use `distinct_id` for user identification in events
- Person properties are denormalized on events for efficiency
- The `$` prefix indicates PostHog-defined properties vs custom ones
"""

    @property
    def description(self) -> str:
        """Return client description with system prompt."""
        text = "PostHog Analytics Client - Query product analytics data using HogQL (SQL-like syntax)."
        return text + "\n\n" + self.system_prompt()


# Alias for dynamic resolution compatibility (posthog -> PosthogClient)
PosthogClient = PostHogClient
