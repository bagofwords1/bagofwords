"""Unit tests for the SharePoint Lists client (graph_list_client).

All Graph traffic is mocked at the `_get` / `_get_with_prefer` level — these
tests cover the connector's own logic: list scoping, column filtering and
dtype mapping, the display→internal name translation that makes OData filters
work on renamed/digit-leading columns, spec parsing, and result flattening.
Shapes mirror real Graph responses captured from a live tenant.
"""
import json
from unittest.mock import patch

import pytest

from app.data_sources.clients.graph_list_client import SharepointListsClient
from app.schemas.data_source_registry import resolve_client_class


SITE_ID = "contoso.sharepoint.com,aaaa,bbbb"

LISTS_RESPONSE = {
    "value": [
        {
            "id": "list-expense",
            "name": "2017_Expense_Data",
            "displayName": "2017_Expense_Data",
            "webUrl": "https://contoso/sites/x/Lists/2017_Expense_Data",
            "list": {"hidden": False, "template": "genericList"},
        },
        {
            "id": "list-hidden",
            "name": "wte",
            "displayName": "Web Template Extensions",
            "list": {"hidden": True, "template": "webTemplateExtensionsList"},
        },
        {
            "id": "list-doclib",
            "name": "Shared Documents",
            "displayName": "Documents",
            "list": {"hidden": False, "template": "documentLibrary"},
        },
        {
            "id": "list-leads",
            "name": "Mock_Salesforce_Leads",
            "displayName": "Mock_Salesforce_Leads",
            "list": {"hidden": False, "template": "genericList"},
        },
    ]
}

# Real-world column shapes: renamed columns keep internal names field_1/field_2;
# the display name "2017" starts with a digit.
EXPENSE_COLUMNS = {
    "value": [
        {"name": "Title", "displayName": "Title", "text": {}},
        {"name": "LinkTitle", "displayName": "", "readOnly": True},
        {"name": "_ColorTag", "displayName": "Color Tag", "readOnly": True, "text": {}},
        {"name": "ComplianceAssetId", "displayName": "Compliance Asset Id", "readOnly": True, "text": {}},
        {"name": "field_1", "displayName": "Category", "text": {}},
        {"name": "field_2", "displayName": "2017", "number": {}},
        {"name": "ID", "displayName": "ID", "readOnly": True},
        {"name": "ContentType", "displayName": "Content Type"},
        {"name": "Modified", "displayName": "Modified", "readOnly": True, "dateTime": {}},
        {"name": "Created", "displayName": "Created", "readOnly": True, "dateTime": {}},
        {"name": "Author", "displayName": "Created By", "readOnly": True, "personOrGroup": {}},
        {"name": "Editor", "displayName": "Modified By", "readOnly": True, "personOrGroup": {}},
        {"name": "Attachments", "displayName": "Attachments"},
        {"name": "ItemChildCount", "displayName": "Item Child Count", "readOnly": True, "lookup": {}},
    ]
}


def make_client(**kwargs):
    client = SharepointListsClient(
        site_url="https://contoso.sharepoint.com/sites/x",
        access_token="tok",
        **kwargs,
    )
    client._site_id = SITE_ID
    return client


def patch_graph(client, items_pages=None):
    """Patch Graph GETs: lists + columns via _get, items via _get_with_prefer."""

    def fake_get(path, **kw):
        if "/lists?" in path or path.endswith("/lists"):
            return LISTS_RESPONSE
        if path.endswith("/columns"):
            return EXPENSE_COLUMNS
        raise AssertionError(f"unexpected _get: {path}")

    pages = list(items_pages or [])

    def fake_get_prefer(url, params):
        return pages.pop(0)

    return (
        patch.object(client, "_get", side_effect=fake_get),
        patch.object(client, "_get_with_prefer", side_effect=fake_get_prefer),
    )


class TestDiscovery:
    def test_included_lists_skips_hidden_and_document_libraries(self):
        client = make_client()
        with patch.object(client, "_get", return_value=LISTS_RESPONSE):
            names = [l["displayName"] for l in client._included_lists()]
        assert names == ["2017_Expense_Data", "Mock_Salesforce_Leads"]

    def test_multiple_lists_per_site_all_cataloged(self):
        client = make_client()
        p1, _ = patch_graph(client)
        with p1:
            tables = client.get_schemas()
        assert [t.name for t in tables] == ["2017_Expense_Data", "Mock_Salesforce_Leads"]

    def test_lists_scope_restricts_by_name(self):
        client = make_client(lists="Mock_Salesforce_Leads")
        with patch.object(client, "_get", return_value=LISTS_RESPONSE):
            names = [l["displayName"] for l in client._included_lists()]
        assert names == ["Mock_Salesforce_Leads"]

    def test_include_hidden_opts_back_in(self):
        client = make_client(include_hidden=True)
        response = {
            "value": LISTS_RESPONSE["value"] + [{
                "id": "list-hidden-generic",
                "name": "HiddenData",
                "displayName": "Hidden Data",
                "list": {"hidden": True, "template": "genericList"},
            }]
        }
        with patch.object(client, "_get", return_value=response):
            names = {l["displayName"] for l in client._included_lists()}
        assert "Hidden Data" in names
        # Excluded templates stay out even with include_hidden on.
        assert "Web Template Extensions" not in names
        assert "Documents" not in names


class TestColumns:
    def test_column_filtering_and_dtypes(self):
        client = make_client()
        with patch.object(client, "_get", return_value=EXPENSE_COLUMNS):
            cols = client._fetch_columns("list-expense")
        by_display = {c["display"]: c for c in cols}
        # user columns present, mapped to display names with dtypes
        assert by_display["Category"]["internal"] == "field_1"
        assert by_display["Category"]["dtype"] == "string"
        assert by_display["2017"]["internal"] == "field_2"
        assert by_display["2017"]["dtype"] == "float"
        assert by_display["ID"]["dtype"] == "integer"
        assert by_display["Modified"]["dtype"] == "datetime"
        # system noise excluded
        for noise in ("Color Tag", "Compliance Asset Id", "Content Type",
                      "Attachments", "Item Child Count"):
            assert noise not in by_display

    def test_schema_carries_internal_name_mapping(self):
        client = make_client()
        p1, _ = patch_graph(client)
        with p1:
            table = client.get_schemas()[0]
        mapping = table.metadata_json["sharepoint_list"]["internal_names"]
        assert mapping["Category"] == "field_1"
        assert mapping["2017"] == "field_2"


class TestFilterTranslation:
    def _cols(self):
        client = make_client()
        with patch.object(client, "_get", return_value=EXPENSE_COLUMNS):
            return client, client._fetch_columns("x")

    def test_display_names_translate_to_internal_with_fields_prefix(self):
        client, cols = self._cols()
        out = client._translate_fields_expr("Category eq 'Expenses'", cols)
        assert out == "fields/field_1 eq 'Expenses'"

    def test_digit_leading_display_name(self):
        client, cols = self._cols()
        out = client._translate_fields_expr("2017 gt 1000", cols)
        assert out == "fields/field_2 gt 1000"

    def test_explicit_fields_prefix_kept(self):
        client, cols = self._cols()
        out = client._translate_fields_expr("fields/Category ne 'X'", cols)
        assert out == "fields/field_1 ne 'X'"

    def test_odata_keywords_untouched(self):
        client, cols = self._cols()
        out = client._translate_fields_expr(
            "Category eq 'A' and 2017 ge 5 or startswith(Title,'B')", cols
        )
        assert out == (
            "fields/field_1 eq 'A' and fields/field_2 ge 5 or startswith(fields/Title,'B')"
        )


class TestSpecParsing:
    def test_bare_string_is_list_name(self):
        assert SharepointListsClient._parse_spec("My List") == {"list": "My List"}

    def test_json_spec(self):
        spec = SharepointListsClient._parse_spec('{"list": "L", "limit": 5}')
        assert spec == {"list": "L", "limit": 5}

    def test_dict_passthrough(self):
        assert SharepointListsClient._parse_spec({"list": "L"}) == {"list": "L"}

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            SharepointListsClient._parse_spec('{"list": ')


class TestExecuteQuery:
    ITEMS_PAGE = {
        "value": [
            {
                "id": "4",
                "createdDateTime": "2026-08-13T18:36:34Z",
                "lastModifiedDateTime": "2026-08-13T18:36:34Z",
                "createdBy": {"user": {"displayName": "Yochay Ettun"}},
                "lastModifiedBy": {"user": {"displayName": "Yochay Ettun"}},
                "fields": {"Title": "21", "field_1": "Total Expenses", "field_2": 5729.5},
            },
            {
                "id": "2",
                "fields": {"Title": "19", "field_1": "Other Expense 1"},  # field_2 omitted (null)
            },
        ]
    }

    def test_query_flattens_to_display_columns(self):
        client = make_client()
        p1, p2 = patch_graph(client, items_pages=[self.ITEMS_PAGE])
        with p1, p2:
            df = client.execute_query(json.dumps({
                "list": "2017_Expense_Data",
                "select": ["Title", "Category", "2017"],
            }))
        assert list(df.columns) == ["id", "Title", "Category", "2017"]
        assert df.iloc[0]["Category"] == "Total Expenses"
        assert df.iloc[0]["2017"] == 5729.5
        # Graph omits null fields — the frame still carries the column
        assert "2017" in df.columns and df.iloc[1]["Category"] == "Other Expense 1"

    def test_filter_translated_in_request(self):
        client = make_client()
        captured = {}

        def fake_get_prefer(url, params):
            captured.update(params or {})
            return {"value": []}

        def fake_get(path, **kw):
            if "/lists" in path and not path.endswith("/columns"):
                return LISTS_RESPONSE
            return EXPENSE_COLUMNS

        with patch.object(client, "_get", side_effect=fake_get), \
             patch.object(client, "_get_with_prefer", side_effect=fake_get_prefer):
            client.execute_query(json.dumps({
                "list": "2017_Expense_Data",
                "filter": "Category ne 'Total Expenses' and 2017 gt 1",
                "select": ["Category", "2017"],
            }))
        assert captured["$filter"] == (
            "fields/field_1 ne 'Total Expenses' and fields/field_2 gt 1"
        )
        assert "field_1" in captured["$expand"] and "field_2" in captured["$expand"]

    def test_unknown_list_lists_available(self):
        client = make_client()
        with patch.object(client, "_get", return_value=LISTS_RESPONSE):
            with pytest.raises(ValueError, match="Available lists"):
                client.execute_query('{"list": "Nope"}')

    def test_unknown_column_lists_available(self):
        client = make_client()
        p1, _ = patch_graph(client)
        with p1:
            with pytest.raises(ValueError, match="Available columns"):
                client.execute_query('{"list": "2017_Expense_Data", "select": ["Bogus"]}')

    def test_person_columns_use_item_envelope(self):
        client = make_client()
        p1, p2 = patch_graph(client, items_pages=[self.ITEMS_PAGE])
        with p1, p2:
            df = client.execute_query(json.dumps({
                "list": "2017_Expense_Data",
                "select": ["Created By"],
            }))
        assert df.iloc[0]["Created By"] == "Yochay Ettun"

    def test_table_name_kwarg_supported(self):
        client = make_client()
        p1, p2 = patch_graph(client, items_pages=[self.ITEMS_PAGE])
        with p1, p2:
            df = client.execute_query(table_name="2017_Expense_Data")
        assert len(df) == 2


class TestRegistryWiring:
    def test_client_resolves_from_registry(self):
        assert resolve_client_class("sharepoint_lists") is SharepointListsClient

    def test_registry_entry_shape(self):
        from app.schemas.data_source_registry import REGISTRY
        entry = REGISTRY["sharepoint_lists"]
        assert entry.data_shape == "tables"
        assert entry.catalog_ownership == "shared"
        assert entry.catalog_nouns == ("list", "lists")
        assert "oauth" in entry.credentials_auth.by_auth

    def test_oauth_service_knows_type(self):
        from app.services.connection_oauth_service import (
            ENTRA_OBO_CONNECTION_TYPES, _OBO_SCOPES,
        )
        assert "sharepoint_lists" in ENTRA_OBO_CONNECTION_TYPES
        assert "sharepoint_lists" in _OBO_SCOPES

    def test_default_user_auth_modes_oauth(self):
        from app.services.connection_service import default_user_auth_modes
        assert default_user_auth_modes("sharepoint_lists", {}, {}) == ["oauth"]

    def test_client_identity(self):
        client = make_client()
        assert client.is_document_based is False
        from app.data_sources.clients.base import Capability
        assert client.capabilities == {Capability.QUERY}
