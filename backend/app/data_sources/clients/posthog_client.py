from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
import logging
import requests
import pandas as pd
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# PostHog rate-limits the query endpoint per project, and an extraction is the
# one thing here that issues many queries back to back. A 429 is a "wait", not a
# failure, so it is waited out rather than surfaced — bounded, so a project that
# is genuinely throttled fails in a minute instead of hanging for the extraction
# window.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 5
RATE_LIMIT_MAX_WAIT_SECONDS = 60


def _posthog_extraction_source(client):
    """Materialize through the HogQL query API, which has no cursor.

    Every read is one bounded HTTP request, so extraction pages with
    LIMIT/OFFSET rather than streaming a result set. See fast/posthog_source.py.
    """
    from app.data_sources.fast.posthog_source import PostHogSource

    return PostHogSource(client)


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

    # Resolved lazily by fast/sources.source_for.
    EXTRACTION_SOURCE = staticmethod(_posthog_extraction_source)

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

    def run_hogql(self, query: str) -> tuple:
        """Execute a HogQL query and return (columns, types, rows) as sent.

        The API's own shape, deliberately: the extraction path pages through
        thousands of rows and needs the column types PostHog reports, and going
        via a DataFrame would drop them and cost a copy per page.
        `execute_query` frames the same result for everyone else.
        """
        payload = {"query": {"kind": "HogQLQuery", "query": query}}

        with self.connect() as session:
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    response = session.post(
                        f"{self.base_url}/query/",
                        json=payload,
                        timeout=120,  # HogQL queries can take time
                    )
                except requests.exceptions.RequestException as e:
                    raise RuntimeError(f"Error executing PostHog query: {e}")

                if response.status_code == 429 and attempt < RATE_LIMIT_RETRIES:
                    wait = _retry_after_seconds(response)
                    logger.info(
                        "posthog.rate_limited",
                        extra={"wait_seconds": wait, "attempt": attempt + 1},
                    )
                    time.sleep(wait)
                    continue
                break

        if response.status_code != 200:
            error_detail = response.text
            try:
                error_json = response.json()
                error_detail = error_json.get("detail", error_json.get("error", response.text))
            except Exception:
                pass
            raise RuntimeError(
                f"PostHog query failed ({response.status_code}): {error_detail}"
            )

        result = response.json()

        # Handle HogQL response format
        if "results" in result and "columns" in result:
            return result["columns"], result.get("types"), result["results"]
        if "error" in result:
            raise RuntimeError(f"HogQL error: {result['error']}")
        raise RuntimeError(f"Unexpected response format: {result}")

    def execute_query(self, query: str, limit: Optional[int] = None) -> pd.DataFrame:
        """Execute a HogQL query and return results as DataFrame.

        Args:
            query: HogQL query string (e.g., "SELECT * FROM events LIMIT 100")
            limit: Optional limit override (applied if query doesn't have LIMIT)

        Returns:
            pandas DataFrame with query results
        """
        columns, _types, rows = self.run_hogql(query)
        return pd.DataFrame(rows, columns=columns)

    def prompt_schema(self) -> str:
        """Generate schema string for LLM prompts."""
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self) -> str:
        """Provide a detailed system prompt for LLM integration."""
        return """
## PostHog Analytics Integration

This connector allows querying PostHog product analytics data using HogQL (PostHog's SQL dialect over ClickHouse).

### How to Query Data

Use the `execute_query` method with a HogQL query string:

```python
# Example: Get daily active users
df = client.execute_query('''
    SELECT toDate(timestamp) as date, count(DISTINCT distinct_id) as dau
    FROM events
    WHERE timestamp >= now() - INTERVAL 7 DAY
    GROUP BY date
    ORDER BY date
''')

# Example: Top events by count
df = client.execute_query('''
    SELECT event, count() as count
    FROM events
    WHERE timestamp >= now() - INTERVAL 30 DAY
    GROUP BY event
    ORDER BY count DESC
    LIMIT 10
''')

# Example: Session metrics by channel
df = client.execute_query('''
    SELECT $channel_type, count() as sessions, avg($session_duration) as avg_duration
    FROM sessions
    WHERE $start_timestamp >= now() - INTERVAL 7 DAY
    GROUP BY $channel_type
''')

# Example: Page views by URL
df = client.execute_query('''
    SELECT properties.$current_url as url, count() as views
    FROM events
    WHERE event = '$pageview' AND timestamp >= now() - INTERVAL 7 DAY
    GROUP BY url
    ORDER BY views DESC
    LIMIT 20
''')
```

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

### Property Access Patterns:
- Event properties: `properties.$property_name` or `properties.custom_property`
- Person properties: `person.properties.$initial_browser`, `person.properties.email`
- Session fields use $ prefix: `$session_duration`, `$is_bounce`, `$channel_type`

### Important Notes:
- ALWAYS use `execute_query("YOUR SQL HERE")` to run queries
- Timestamps use ClickHouse functions: `now()`, `toDate()`, `INTERVAL N DAY/HOUR/MONTH`
- Default query limit is 100 rows; use `LIMIT` clause for more
- Use `distinct_id` for user identification in events
- The `$` prefix indicates PostHog-defined properties vs custom ones
"""

    @property
    def description(self) -> str:
        """Return client description with system prompt."""
        text = "PostHog Analytics Client - Query product analytics data using HogQL (SQL-like syntax)."
        return text + "\n\n" + self.system_prompt()


def _retry_after_seconds(response) -> int:
    """How long a 429 asks us to wait, bounded and never zero.

    PostHog sends `Retry-After` in seconds; the header is optional and has been
    seen carrying an HTTP date, so anything unparseable falls back to a fixed
    wait rather than retrying immediately and burning the next attempt.
    """
    raw = (response.headers or {}).get("Retry-After")
    try:
        wait = int(float(raw))
    except (TypeError, ValueError):
        wait = RATE_LIMIT_DEFAULT_WAIT_SECONDS
    return max(1, min(wait, RATE_LIMIT_MAX_WAIT_SECONDS))


# Alias for dynamic resolution compatibility (posthog -> PosthogClient)
PosthogClient = PostHogClient
