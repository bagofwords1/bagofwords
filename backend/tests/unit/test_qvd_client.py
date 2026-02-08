"""
Unit tests for QVDClient

Tests the QVD loading/querying path including:
- Reading .qvd files via pyqvd
- Registering in DuckDB
- Schema retrieval
- Query execution
"""
import os
import pytest
from app.data_sources.clients.qvd_client import QVDClient


# Get absolute path to test fixtures
TEST_DIR = os.path.dirname(os.path.dirname(__file__))
FIXTURES_DIR = os.path.join(TEST_DIR, "config")
TEST_SOURCE_QVD = os.path.join(FIXTURES_DIR, "test_source.qvd")
PRODUCTS_SOURCE_QVD = os.path.join(FIXTURES_DIR, "products_source.qvd")


class TestQVDClient:
    """Test suite for QVDClient functionality"""

    def test_single_file_instantiation(self):
        """Test that QVDClient can be instantiated with a single file path"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        assert client is not None
        assert len(client.patterns) == 1
        assert client.patterns[0] == TEST_SOURCE_QVD

    def test_multiple_files_instantiation(self):
        """Test that QVDClient can be instantiated with multiple file paths"""
        file_paths = f"{TEST_SOURCE_QVD}\n{PRODUCTS_SOURCE_QVD}"
        client = QVDClient(file_paths=file_paths)
        assert client is not None
        assert len(client.patterns) == 2

    def test_glob_pattern_instantiation(self):
        """Test that QVDClient can be instantiated with glob patterns"""
        pattern = os.path.join(FIXTURES_DIR, "*.qvd")
        client = QVDClient(file_paths=pattern)
        files = client._resolve_files()
        assert len(files) >= 2
        assert any("test_source.qvd" in f for f in files)
        assert any("products_source.qvd" in f for f in files)

    def test_connection_context_manager(self):
        """Test that the connection context manager works correctly"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        with client.connect() as con:
            assert con is not None
            # Verify connection is active
            result = con.execute("SELECT 1 as test").fetchall()
            assert result[0][0] == 1

    def test_get_schemas(self):
        """Test that get_schemas returns table information"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        schemas = client.get_schemas()
        
        assert len(schemas) == 1
        table = schemas[0]
        
        # Verify table has a name
        assert hasattr(table, 'name')
        assert table.name == "test_source"
        
        # Verify table has columns
        assert hasattr(table, 'columns')
        assert len(table.columns) > 0
        
        # Check that columns have expected structure
        col = table.columns[0]
        assert hasattr(col, 'name')
        assert hasattr(col, 'dtype')
        
        # Verify some expected columns from test_source.qvd
        col_names = [c.name for c in table.columns]
        assert "AddressNumber" in col_names
        assert "ItemNumber" in col_names
        assert "ItemDesc" in col_names

    def test_get_schemas_multiple_files(self):
        """Test that get_schemas works with multiple QVD files"""
        file_paths = f"{TEST_SOURCE_QVD}\n{PRODUCTS_SOURCE_QVD}"
        client = QVDClient(file_paths=file_paths)
        schemas = client.get_schemas()
        
        assert len(schemas) == 2
        table_names = [t.name for t in schemas]
        assert "test_source" in table_names
        assert "products_source" in table_names

    def test_get_schema_single_table(self):
        """Test that get_schema returns information for a specific table"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        schema = client.get_schema("test_source")
        
        assert schema.name == "test_source"
        assert len(schema.columns) > 0
        
        # Verify metadata includes source file
        assert hasattr(schema, 'metadata_json')
        assert 'qvd' in schema.metadata_json
        assert 'source_file' in schema.metadata_json['qvd']

    def test_execute_query_simple_select(self):
        """Test that execute_query can run a simple SELECT query"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        df = client.execute_query("SELECT * FROM test_source LIMIT 5")
        
        assert df is not None
        assert len(df) == 5
        assert "AddressNumber" in df.columns
        assert "ItemDesc" in df.columns

    def test_execute_query_aggregation(self):
        """Test that execute_query can run aggregation queries"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        df = client.execute_query("SELECT COUNT(*) as row_count FROM test_source")
        
        assert df is not None
        assert len(df) == 1
        assert "row_count" in df.columns
        assert df["row_count"].iloc[0] == 120  # Based on fixture inspection

    def test_execute_query_with_filter(self):
        """Test that execute_query can run queries with WHERE clauses"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        df = client.execute_query(
            "SELECT * FROM test_source WHERE AddressNumber = 10022755"
        )
        
        assert df is not None
        assert len(df) > 0
        # Verify all rows match the filter
        assert all(df["AddressNumber"] == 10022755)

    def test_execute_query_multiple_tables(self):
        """Test that execute_query can query multiple tables"""
        file_paths = f"{TEST_SOURCE_QVD}\n{PRODUCTS_SOURCE_QVD}"
        client = QVDClient(file_paths=file_paths)
        
        # Query each table separately
        df1 = client.execute_query("SELECT COUNT(*) as cnt FROM test_source")
        df2 = client.execute_query("SELECT COUNT(*) as cnt FROM products_source")
        
        assert df1["cnt"].iloc[0] == 120
        assert df2["cnt"].iloc[0] == 77

    def test_test_connection_success(self):
        """Test that test_connection returns success for valid files"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        result = client.test_connection()
        
        assert result["success"] is True
        assert "Successfully loaded" in result["message"]
        assert "1 QVD file" in result["message"]

    def test_test_connection_multiple_files(self):
        """Test that test_connection reports multiple files correctly"""
        file_paths = f"{TEST_SOURCE_QVD}\n{PRODUCTS_SOURCE_QVD}"
        client = QVDClient(file_paths=file_paths)
        result = client.test_connection()
        
        assert result["success"] is True
        assert "2 QVD file" in result["message"]

    def test_test_connection_no_files(self):
        """Test that test_connection fails gracefully when no files are found"""
        client = QVDClient(file_paths="/nonexistent/*.qvd")
        result = client.test_connection()
        
        assert result["success"] is False
        assert "No QVD files found" in result["message"]

    def test_safe_table_name_generation(self):
        """Test that table names are generated safely from file paths"""
        client = QVDClient(file_paths="")
        used = set()
        
        # Test basic name
        name1 = client._safe_table_name("/path/to/MyFile.qvd", used)
        assert name1 == "myfile"
        
        # Test duplicate handling
        name2 = client._safe_table_name("/other/MyFile.qvd", used)
        assert name2 == "myfile_2"
        
        # Test special characters
        name3 = client._safe_table_name("/path/My-File@123.qvd", used)
        assert name3 == "my_file_123"

    def test_description_property(self):
        """Test that the description property provides useful information"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        description = client.description
        
        assert "test_source.qvd" in description
        assert "SQL" in description
        assert "DuckDB" in description

    def test_prompt_schema(self):
        """Test that prompt_schema returns formatted schema information"""
        client = QVDClient(file_paths=TEST_SOURCE_QVD)
        schema_str = client.prompt_schema()
        
        assert schema_str is not None
        assert len(schema_str) > 0
        # Should contain table and column information
        assert "test_source" in schema_str.lower() or "AddressNumber" in schema_str


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
