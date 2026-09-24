"""Unit tests for InforEpmClient — the HTTP boundary (ION token endpoint and
the Application Engine REST API) is mocked; everything else runs real."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

from app.data_sources.clients.infor_epm_client import (
    InforEpmClient,
    InforEpmError,
    parse_bow_schema,
    parse_mdx_cells,
)

FIXTURES = Path(__file__).parent / "fixtures" / "infor_epm"
API = "http://epm.test/api/rest/BowService/v1"
TOKEN_URL = "http://epm.test/token"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeEngine:
    """Routes the client's HTTP calls the way a gateway + Application Engine
    would: token exchange, sync/async process invocation, getasyncresult."""

    def __init__(self, processes=None, token="tok-1", first_poll_running=True, engine_errors=None):
        self.processes = processes or {}
        self.token = token
        self.first_poll_running = first_poll_running
        self.engine_errors = engine_errors or {}
        self.calls = []  # (url, payload, headers)
        self.token_requests = 0
        self._tasks = {}
        self._polls = {}

    # -- requests.post (token endpoint) ---------------------------------------
    def token_post(self, url, data=None, **kwargs):
        self.token_requests += 1
        resp = MagicMock()
        if data.get("client_secret") != "demo-secret":
            resp.status_code = 401
            resp.json.return_value = {"error": "invalid_client"}
            return resp
        resp.status_code = 200
        resp.json.return_value = {"access_token": f"{self.token}-{self.token_requests}", "expires_in": 3600}
        return resp

    # -- Session.post (engine) --------------------------------------------------
    def session_post(self, session, url, json=None, headers=None, **kwargs):
        auth = dict(session.headers).get("Authorization", "")
        self.calls.append((url, json, auth))
        resp = MagicMock()
        if not auth.startswith("Bearer "):
            resp.status_code = 401
            resp.text = "unauthorized"
            return resp
        rest = url[len(API) + 1:]
        if rest == "getasyncresult":
            tid = json["taskId"]
            if self.first_poll_running and self._polls.get(tid, 0) == 0:
                self._polls[tid] = 1
                resp.status_code = 200
                resp.json.return_value = {"status": "Running", "taskId": tid}
                return resp
            resp.status_code = 200
            resp.json.return_value = {"status": "Completed", "taskId": tid, **self._tasks[tid]}
            return resp
        is_async = rest.endswith("/async")
        name = rest[:-6] if is_async else rest
        if name in self.engine_errors:
            resp.status_code = 200
            resp.json.return_value = {"error": self.engine_errors[name]}
            return resp
        fn = self.processes.get(name)
        if fn is None:
            resp.status_code = 404
            resp.text = "not published"
            return resp
        result = fn(json)
        if is_async:
            tid = f"task-{len(self._tasks) + 1}"
            self._tasks[tid] = {"result": result}
            resp.status_code = 200
            resp.json.return_value = {"taskId": tid}
            return resp
        resp.status_code = 200
        resp.json.return_value = {"result": result}
        return resp


@pytest.fixture
def engine():
    eng = FakeEngine(processes={
        "BOW_GetCubeList": lambda p: "Sales\nFinance\n#SysConfig\n",
        "BOW_GetCubeSchema": lambda p: _fixture("cube_schema.xml") if p["CubeName"] == "Sales" else _fixture("cube_schema_odbo_measures.xml"),
        "BOW_ExecuteMdx": lambda p: _fixture("mdx_result.txt"),
    })
    with patch("app.data_sources.clients.infor_epm_client.requests.post", side_effect=eng.token_post), \
         patch.object(requests.Session, "post", autospec=True, side_effect=eng.session_post):
        yield eng


def _client(**overrides):
    kwargs = {
        "api_url": API, "olap_database": "DEMO_OLAP", "gateway_token_url": TOKEN_URL,
        "gateway_client_id": "demo-client", "gateway_client_secret": "demo-secret", "poll_interval_sec": 0.05,
    }
    kwargs.update(overrides)
    return InforEpmClient(**kwargs)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_resolves_client_and_form_fields():
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    assert resolve_client_class("infor_epm") is InforEpmClient
    entry = REGISTRY["infor_epm"]
    fields = set(entry.config_schema.model_fields)
    assert {"api_url", "olap_database", "max_rows", "async_mode"} <= fields
    assert "ion_oauth" in entry.credentials_auth.by_auth
    # No username/password variant: the whole point of this connector.
    for variant in entry.credentials_auth.by_auth.values():
        assert "password" not in variant.schema.model_fields


# ---------------------------------------------------------------------------
# Auth / connection
# ---------------------------------------------------------------------------

class TestConnect:
    def test_exchanges_client_credentials_and_sends_bearer(self, engine):
        c = _client()
        result = c.test_connection()
        assert result["success"] is True
        assert result["cubes"] == 2
        assert engine.token_requests == 1
        assert all(auth.startswith("Bearer tok-1") for _, _, auth in engine.calls)

    def test_static_bearer_token_skips_exchange(self, engine):
        c = InforEpmClient(api_url=API, olap_database="DEMO_OLAP", bearer_token="demo-bearer-token")
        assert c.test_connection()["success"] is True
        assert engine.token_requests == 0
        assert engine.calls[0][2] == "Bearer demo-bearer-token"

    def test_rejected_credentials_are_reported_as_auth_failure(self, engine):
        result = _client(gateway_client_secret="wrong").test_connection()
        assert result["success"] is False
        assert "401" in result["message"]

    def test_unpublished_process_is_reported_as_path_problem(self, engine):
        result = _client(cube_list_process="BOW_Missing").test_connection()
        assert result["success"] is False
        assert "404" in result["message"]

    def test_missing_config_is_a_configuration_error(self):
        result = InforEpmClient(api_url=API, olap_database="DEMO_OLAP").test_connection()
        assert result["success"] is False
        assert "gateway_token_url" in result["message"]

    def test_expired_token_is_refreshed_once_on_401(self, engine):
        c = _client()
        c.connect()
        # Simulate the gateway invalidating the token: drop the header so the
        # fake engine answers 401, then the client must re-exchange and retry.
        c._http.headers.pop("Authorization")
        c._token_expires_at = 10 ** 12  # not locally expired, so the 401 drives the refresh
        assert c._list_cubes() == ["Sales", "Finance"]
        assert engine.token_requests == 2


# ---------------------------------------------------------------------------
# Schema discovery
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_one_table_per_queryable_cube_with_process_params(self, engine):
        tables = _client().get_schemas()
        assert [t.name for t in tables] == ["DEMO_OLAP/Sales", "DEMO_OLAP/Finance"]
        schema_calls = [(u, p) for u, p, _ in engine.calls if "BOW_GetCubeSchema" in u]
        assert {p["CubeName"] for _, p in schema_calls} == {"Sales", "Finance"}
        assert all(p["OLAPName"] == "DEMO_OLAP" for _, p in schema_calls)

    def test_dimensions_become_columns_in_cube_order_with_mdx_metadata(self, engine):
        sales = _client().get_schemas()[0]
        dims = [c for c in sales.columns if c.dtype == "dimension"]
        assert [d.name for d in dims] == ["Period", "Region", "SalesMeasures"]
        period = dims[0]
        assert period.metadata["unique_name"] == "[Period]"
        assert period.metadata["is_time_dimension"] is True
        assert period.metadata["element_count"] == 11
        assert "2024-Q1" in period.metadata["sample_elements"]
        assert "[Period].[2024]" in period.metadata["sample_unique_names"]
        assert period.metadata["hierarchies"][0]["levels"] == ["Total", "Year", "Quarter"]
        assert period.description == "Fiscal periods"
        assert sales.description == "Sales by region and product"
        assert sales.metadata_json["infor_epm"]["cubeUniqueName"] == "[Sales]"

    def test_measure_dimension_by_name_pattern_exposes_measure_columns(self, engine):
        sales = _client().get_schemas()[0]
        measures = [c for c in sales.columns if c.dtype == "measure"]
        assert {m.name for m in measures} == {"Revenue", "Units", "Cost"}
        assert measures[0].metadata["unique_name"] == "[SalesMeasures].[Revenue]"
        assert sales.metadata_json["infor_epm"]["measure_dimension"] == "SalesMeasures"
        dim = next(c for c in sales.columns if c.name == "SalesMeasures")
        assert dim.metadata["is_measure_dimension"] is True

    def test_measure_dimension_by_odbo_type_wins_over_name_pattern(self, engine):
        finance = _client(measure_dimension_pattern="account").get_schemas()[1]
        assert finance.metadata_json["infor_epm"]["measure_dimension"] == "Values"
        assert {c.name for c in finance.columns if c.dtype == "measure"} == {"Amount", "Budget"}
        # Sections are re-ordered by cube position, not document order.
        assert [c.name for c in finance.columns if c.dtype == "dimension"] == ["Values", "Account"]

    def test_progress_callback_reports_each_cube(self, engine):
        seen = []
        _client().get_schemas(progress_callback=lambda ph, item, done, total: seen.append((item, done, total)))
        assert ("Sales", 1, 2) in seen and ("Finance", 2, 2) in seen

    def test_get_schema_resolves_by_table_or_cube_name(self, engine):
        c = _client()
        assert c.get_schema("DEMO_OLAP/Finance").name == "DEMO_OLAP/Finance"
        assert c.get_schema("Sales").name == "DEMO_OLAP/Sales"
        with pytest.raises(RuntimeError):
            c.get_schema("Nope")


class TestBowSchemaParser:
    def test_tolerates_embedded_prologs_and_alea_error_nodes(self):
        parsed = parse_bow_schema(_fixture("cube_schema.xml"))
        assert parsed["cube"]["description"] == "Sales by region and product"
        period = parsed["dimensions"][0]
        assert period["name"] == "Period" and period["odbo_type"] == "1"
        assert period["default_hierarchy"] == "Period"
        assert period["elements"][:2] == ["All", "2024"]

    def test_rejects_non_schema_payloads(self):
        with pytest.raises(InforEpmError):
            parse_bow_schema("<Other/>")
        with pytest.raises(InforEpmError):
            parse_bow_schema("not xml at all <")


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

class TestExecuteQuery:
    MDX = "SELECT {[SalesMeasures].[Revenue]} ON COLUMNS, [Region].Members ON ROWS FROM [Sales]"

    def test_async_flow_polls_until_completed_and_parses_cells(self, engine):
        df = _client().execute_query(self.MDX, "DEMO_OLAP/Sales")
        assert list(df.columns) == ["SalesMeasures", "Region", "value"]
        assert len(df) == 3
        assert df.loc[1, "value"] == pytest.approx(140382.5)
        assert pd.api.types.is_numeric_dtype(df["value"])
        assert df.attrs["truncated"] is True
        urls = [u for u, _, _ in engine.calls]
        assert any(u.endswith("BOW_ExecuteMdx/async") for u in urls)
        assert sum(1 for u in urls if u.endswith("getasyncresult")) == 2

    def test_sends_mdx_and_row_limit_to_process(self, engine):
        _client(max_rows=777).execute_query(self.MDX, max_rows=5)
        _, payload, _ = next(c for c in engine.calls if "BOW_ExecuteMdx" in c[0])
        assert payload["mdxQuery"] == self.MDX
        assert payload["rowLimit"] == 5
        assert payload["OLAPName"] == "DEMO_OLAP"

    def test_default_row_limit_comes_from_config(self, engine):
        _client(max_rows=777).execute_query(self.MDX)
        _, payload, _ = next(c for c in engine.calls if "BOW_ExecuteMdx" in c[0])
        assert payload["rowLimit"] == 777

    def test_sync_mode_calls_process_without_async_suffix(self, engine):
        df = _client(async_mode=False).execute_query(self.MDX)
        assert len(df) == 3
        assert not any(u.endswith("/async") or u.endswith("getasyncresult") for u, _, _ in engine.calls)

    def test_bow_error_from_process_raises(self, engine):
        engine.processes["BOW_ExecuteMdx"] = lambda p: "BOW_ERROR: Unknown dimension [Nope]"
        with pytest.raises(InforEpmError, match="Unknown dimension"):
            _client().execute_query("SELECT [Nope].Members ON COLUMNS FROM [Sales]")

    def test_engine_level_error_json_raises(self, engine):
        engine.engine_errors["BOW_ExecuteMdx"] = "An error has occurred: Object reference not set"
        with pytest.raises(InforEpmError, match="Object reference"):
            _client().execute_query(self.MDX)

    def test_no_cells_is_an_empty_frame_not_an_error(self, engine):
        engine.processes["BOW_ExecuteMdx"] = lambda p: "BOW_ERROR: MDX returned no cells"
        df = _client().execute_query("SELECT {} ON COLUMNS FROM [Sales]")
        assert df.empty

    def test_empty_query_is_rejected_locally(self, engine):
        with pytest.raises(ValueError):
            _client().execute_query("   ")
        assert engine.calls == []

    def test_text_cells_stay_strings(self, engine):
        engine.processes["BOW_ExecuteMdx"] = lambda p: _fixture("mdx_result_text.txt")
        df = _client().execute_query("SELECT [Settings].[Label] ON COLUMNS FROM [Settings]")
        assert list(df.columns) == ["Settings", "value"]
        assert df.loc[0, "value"] == "J91"


class TestMdxCellParser:
    def test_duplicate_dimensions_get_suffixed_columns(self):
        df, truncated = parse_mdx_cells("[Region].[Region].[North]\t[Region].[Region].[South]\tNUM\t1\n")
        assert list(df.columns) == ["Region", "Region_2", "value"]
        assert truncated is False

    def test_decorative_lines_are_ignored(self):
        text = "=====\nROW | COORDINATES\n[Region].[Region].[North]\tNUM\t3\n"
        df, _ = parse_mdx_cells(text)
        assert len(df) == 1 and df.loc[0, "Region"] == "North"
