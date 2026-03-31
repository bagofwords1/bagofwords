from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from typing import List, Optional
import requests
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class TimbrA2aClient(DataSourceClient):
    """
    Timbr Agent-to-Agent client.

    Sends natural-language prompts to the Timbr ``/openapi/execute/prompt``
    endpoint, which internally resolves concepts, generates SQL, executes it,
    and returns the result rows.
    """

    def __init__(
        self,
        host: str,
        ontology: str,
        api_key: str,
        verify_ssl: bool = True,
        # Tuning defaults (map to request headers)
        results_limit: int = 500,
        graph_depth: int = 1,
        retries: int = 3,
        retry_if_no_results: bool = True,
        no_results_max_retries: int = 2,
        db_is_case_sensitive: bool = False,
        include_logic_concepts: bool = False,
        should_validate_sql: bool = True,
        enable_reasoning: bool = False,
        reasoning_steps: int = 2,
    ):
        self.host = host.rstrip("/")
        self.ontology = ontology
        self.api_key = api_key
        self.verify_ssl = verify_ssl

        # Tuning knobs
        self.results_limit = results_limit
        self.graph_depth = graph_depth
        self.retries = retries
        self.retry_if_no_results = retry_if_no_results
        self.no_results_max_retries = no_results_max_retries
        self.db_is_case_sensitive = db_is_case_sensitive
        self.include_logic_concepts = include_logic_concepts
        self.should_validate_sql = should_validate_sql
        self.enable_reasoning = enable_reasoning
        self.reasoning_steps = reasoning_steps

        self._base_url = f"{self.host}/timbr/openapi"
        self._session: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
                "accept": "*/*",
            })
            self._session.verify = self.verify_ssl
        return self._session

    def connect(self):
        """No-op – session is created lazily on first request."""
        pass

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _prompt_headers(self) -> dict:
        """Build the per-request headers for the execute/prompt endpoint."""
        return {
            "ontology": self.ontology,
            "x-async": "false",
            "additional-parameters": "",
            "include-logic-concepts": str(self.include_logic_concepts).lower(),
            "should-validate-sql": str(self.should_validate_sql).lower(),
            "retries": str(self.retries),
            "retry-if-no-results": str(self.retry_if_no_results).lower(),
            "no-results-max-retries": str(self.no_results_max_retries),
            "db-is-case-sensitive": str(self.db_is_case_sensitive).lower(),
            "graph-depth": str(self.graph_depth),
            "enable-reasoning": str(self.enable_reasoning).lower(),
            "reasoning-steps": str(self.reasoning_steps),
            "results-limit": str(self.results_limit),
        }

    def _execute_prompt(self, prompt: str, timeout: int = 120) -> dict:
        """POST a natural-language prompt and return the full response data."""
        session = self._get_session()
        url = f"{self._base_url}/execute/prompt"
        resp = session.post(
            url,
            json={"prompt": prompt},
            headers=self._prompt_headers(),
            timeout=timeout,
        )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Timbr A2A error: HTTP {resp.status_code} {resp.text}"
            )
        body = resp.json()
        if body.get("status") != "success":
            error_msg = (body.get("data") or {}).get("error") or body
            raise RuntimeError(f"Timbr A2A returned non-success: {error_msg}")
        return body.get("data", {})

    # ------------------------------------------------------------------
    # DataSourceClient interface
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """
        Validate connectivity via GET /execute/show_ontologies.
        """
        try:
            session = self._get_session()
            url = f"{self._base_url}/execute/show_ontologies"
            resp = session.get(
                url,
                headers={
                    "results-limit": "100",
                    "results-offset": "0",
                },
                timeout=30,
            )
            if resp.status_code >= 300:
                return {
                    "success": False,
                    "message": f"Timbr A2A error: HTTP {resp.status_code} {resp.text}",
                }
            body = resp.json()

            # Response is a list of ontology objects
            ontology_names = []
            if isinstance(body, list):
                ontology_names = [o.get("ontology", "") for o in body]
            elif isinstance(body, dict) and body.get("status") == "success":
                ontology_names = [o.get("ontology", "") for o in body.get("data", [])]

            if self.ontology not in ontology_names:
                return {
                    "success": False,
                    "message": (
                        f"Connected to Timbr A2A but ontology '{self.ontology}' "
                        f"not found. Available: {', '.join(ontology_names)}"
                    ),
                }

            return {
                "success": True,
                "message": f"Connected to Timbr A2A. Ontology '{self.ontology}' found.",
            }
        except requests.exceptions.ConnectionError as e:
            return {
                "success": False,
                "message": f"Cannot reach Timbr A2A at {self.host}: {e}",
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # Schema discovery
    # ------------------------------------------------------------------

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def get_tables(self) -> List[Table]:
        """Return placeholder agent list."""
        return [
            Table(
                name="agent",
                description="Timbr A2A agent – accepts natural language prompts",
                columns=[],
                pks=[],
                fks=[],
                is_active=True,
                metadata_json={
                    "timbr_a2a": {
                        "ontology": self.ontology,
                        "type": "agent",
                    }
                },
            )
        ]

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_tables():
            if t.name == table_name:
                return t
        raise RuntimeError(f"'{table_name}' not found")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(self, query: str) -> pd.DataFrame:
        """
        Send *query* as a natural-language prompt to Timbr A2A
        and return the result rows as a DataFrame.
        """
        if not query or not query.strip():
            raise ValueError("Prompt is required")

        data = self._execute_prompt(query)
        rows = data.get("rows", [])

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # LLM prompt helpers
    # ------------------------------------------------------------------

    def prompt_schema(self) -> str:
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    @property
    def description(self) -> str:
        text = f"Timbr A2A agent – ontology '{self.ontology}' at {self.host}"
        text += "\n\n" + self.system_prompt()
        return text

    def system_prompt(self) -> str:
        return f"""## Timbr A2A (Agent-to-Agent) Query Guide

This data source is powered by a Timbr AI agent. You do NOT write SQL —
instead, send a natural-language question and the agent returns the answer.

If the user asks about questions that you don't know how to answer- use this tool to write a prompt and get data
use create_data with the Timbr A2A to get the data you need.

Ontology: `{self.ontology}`

### How to query

Build a `query_text` that includes conversation context so the Timbr agent
can resolve references like "those", "the same customers", "filter that further", etc.

Always construct the prompt as:
1. **Context block** — a short summary of what was previously asked and returned (if any).
2. **Current question** — the user's new question.

```python
# Simple retrieval
df = timbr_a2a_client.execute_query("show me the last 5 conversations")

# Analytical / aggregation
df = timbr_a2a_client.execute_query("how many conversations happened per day in the last week?")

# Filtered query
df = timbr_a2a_client.execute_query("list all customers who had more than 3 conversations this month")

# Top-N / ranking
df = timbr_a2a_client.execute_query("which branches have the most conversations? show top 10")

# Follow-up with conversation context
query_text = (
    "Context: Previously retrieved the last 5 conversations with columns "
    "conversation_reference_number, conversation_date, conversation_content. "
    "The results showed conversations from 2025-07-02 about deposits and savings.\\n\\n"
    "Current question: of those, show only conversations that mention deposits"
)
df = timbr_a2a_client.execute_query(query_text)
```

### Important Rules

1. Write clear, specific natural-language questions — not SQL.
2. The agent resolves the right concepts and generates SQL internally.
3. Results are returned as a pandas DataFrame.
4. Be specific about filters, limits, and columns you care about.
5. You can reference domain concepts naturally (e.g. "customers", "orders").
6. **ALWAYS include conversation context** when the user's question is a follow-up. Summarize what was previously asked and what the key results were so the Timbr agent can understand references."""
