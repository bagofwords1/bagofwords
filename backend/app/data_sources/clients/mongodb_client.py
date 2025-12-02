from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from pymongo import MongoClient
from bson import ObjectId
import pandas as pd
import json
from typing import List, Optional, Generator
from contextlib import contextmanager
from datetime import datetime


class MongodbClient(DataSourceClient):
    """MongoDB client for document-based data access."""
    
    def __init__(
        self,
        host: str,
        port: int = 27017,
        database: str = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        auth_source: str = "admin",
        tls: bool = False,
    ):
        self.host = host
        self.port = port
        self.database_name = database
        self.user = user
        self.password = password
        self.auth_source = auth_source
        self.tls = tls
        self._client = None
    
    @property
    def is_document_based(self) -> bool:
        """Mark this as a document-based data source."""
        return True
    
    def _build_uri(self) -> str:
        """Build MongoDB connection URI."""
        if self.user and self.password:
            from urllib.parse import quote_plus
            user = quote_plus(self.user)
            password = quote_plus(self.password)
            # Only include authSource if it's not empty
            auth_source = self.auth_source if self.auth_source else "admin"
            return f"mongodb://{user}:{password}@{self.host}:{self.port}/{self.database_name}?authSource={auth_source}"
        return f"mongodb://{self.host}:{self.port}/{self.database_name}"
    
    @contextmanager
    def connect(self) -> Generator:
        """Context manager for MongoDB connection."""
        client = None
        try:
            client = MongoClient(self._build_uri(), tls=self.tls)
            yield client[self.database_name]
        finally:
            if client:
                client.close()
    
    def execute_query(self, query: str) -> pd.DataFrame:
        """
        Execute MongoDB query and return results as DataFrame.
        
        Query format (JSON string):
        {
            "collection": "orders",
            "find": {"status": "active"},      # OR
            "aggregate": [...pipeline...],     # aggregation pipeline
            "limit": 100,
            "sort": {"created_at": -1}
        }
        """
        try:
            query_dict = json.loads(query)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON query: {e}")
        
        collection_name = query_dict.get("collection")
        if not collection_name:
            raise ValueError("Query must specify 'collection'")
        
        with self.connect() as db:
            collection = db[collection_name]
            
            if "aggregate" in query_dict:
                # Aggregation pipeline
                pipeline = query_dict["aggregate"]
                cursor = collection.aggregate(pipeline)
                results = list(cursor)
            else:
                # Find query
                filter_query = query_dict.get("find", {})
                projection = query_dict.get("projection")
                limit = query_dict.get("limit", 100)
                sort = query_dict.get("sort")
                
                cursor = collection.find(filter_query, projection)
                if sort:
                    cursor = cursor.sort(list(sort.items()))
                if limit:
                    cursor = cursor.limit(limit)
                
                results = list(cursor)
        
        # Convert BSON types to JSON-serializable
        for doc in results:
            self._convert_bson_types(doc)
        
        # Return as DataFrame
        if not results:
            return pd.DataFrame()
        
        # Create DataFrame from documents - this gives us columns for each top-level field
        # Nested objects become dict values in cells, arrays stay as lists
        df = pd.DataFrame(results)
        
        return df
    
    def _convert_bson_types(self, doc: dict) -> None:
        """Recursively convert BSON types to JSON-serializable types."""
        for key, value in list(doc.items()):
            if isinstance(value, ObjectId):
                doc[key] = str(value)
            elif isinstance(value, datetime):
                doc[key] = value.isoformat()
            elif isinstance(value, bytes):
                doc[key] = value.decode('utf-8', errors='replace')
            elif isinstance(value, dict):
                self._convert_bson_types(value)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        self._convert_bson_types(item)
                    elif isinstance(item, ObjectId):
                        value[i] = str(item)
                    elif isinstance(item, datetime):
                        value[i] = item.isoformat()
    
    def _get_all_keys(self, collection, sample_size: int = 100) -> dict:
        """Get union of all keys from sampled documents, with sample values for type inference.
        
        Returns a merged document containing all keys found across samples,
        with representative values for each key.
        """
        try:
            # Sample documents and merge their structures
            samples = list(collection.aggregate([{"$sample": {"size": sample_size}}]))
            if not samples:
                sample = collection.find_one()
                return sample if sample else {}
            
            # Merge all samples into a single "super document" with all keys
            merged = {}
            self._merge_docs(merged, samples)
            return merged
        except Exception:
            # Fallback: just get one document
            return collection.find_one() or {}
    
    def _merge_docs(self, merged: dict, docs: List[dict], prefix: str = "") -> None:
        """Recursively merge multiple documents to capture all unique keys."""
        for doc in docs:
            for key, value in doc.items():
                if key not in merged:
                    # First time seeing this key - store value for type inference
                    merged[key] = value
                elif isinstance(value, dict) and isinstance(merged[key], dict):
                    # Both are dicts - recurse to merge nested keys
                    self._merge_docs(merged[key], [value])
                elif isinstance(value, list) and value and isinstance(value[0], dict):
                    # Array of objects - merge array element structures
                    if isinstance(merged[key], list) and merged[key] and isinstance(merged[key][0], dict):
                        self._merge_docs(merged[key][0], [value[0]])
                    else:
                        merged[key] = value

    def get_tables(self) -> List[Table]:
        """Get all collections and their inferred schema."""
        tables = []
        with self.connect() as db:
            for coll_name in db.list_collection_names():
                # Sample multiple docs and merge all unique keys
                merged_sample = self._get_all_keys(db[coll_name])
                columns = []
                if merged_sample:
                    self._convert_bson_types(merged_sample)
                    columns = self._infer_columns(merged_sample)
                
                tables.append(Table(
                    name=coll_name,
                    columns=columns,
                    pks=[TableColumn(name="_id", dtype="string")],
                    fks=[],
                    metadata_json={"type": "collection"}
                ))
        return tables
    
    def _infer_columns(self, doc: dict, prefix: str = "") -> List[TableColumn]:
        """Infer column types from a sample document, including array element structure."""
        columns = []
        for key, value in doc.items():
            full_key = f"{prefix}.{key}" if prefix else key
            
            if isinstance(value, dict):
                # Nested object - recurse
                columns.extend(self._infer_columns(value, full_key))
            elif isinstance(value, list):
                # Array - add array column and also inspect first element if it's a dict
                columns.append(TableColumn(name=full_key, dtype="array"))
                if value and isinstance(value[0], dict):
                    # Show array element structure with [] notation
                    columns.extend(self._infer_columns(value[0], f"{full_key}[]"))
            else:
                # Determine type for scalar values
                dtype = "object"
                if isinstance(value, bool):
                    dtype = "boolean"
                elif isinstance(value, int):
                    dtype = "integer"
                elif isinstance(value, float):
                    dtype = "number"
                elif isinstance(value, str):
                    dtype = "string"
                
                columns.append(TableColumn(name=full_key, dtype=dtype))
        
        return columns
    
    def get_schemas(self) -> List[Table]:
        """Get schemas for all collections."""
        return self.get_tables()
    
    def get_schema(self, collection_name: str) -> Table:
        """Get schema for a specific collection."""
        with self.connect() as db:
            merged_sample = self._get_all_keys(db[collection_name])
            columns = []
            if merged_sample:
                self._convert_bson_types(merged_sample)
                columns = self._infer_columns(merged_sample)
            return Table(
                name=collection_name,
                columns=columns,
                pks=[TableColumn(name="_id", dtype="string")],
                fks=[]
            )
    
    def prompt_schema(self) -> str:
        """Generate schema prompt for LLM."""
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str
    
    def test_connection(self):
        """Test MongoDB connection."""
        try:
            with self.connect() as db:
                db.list_collection_names()
                return {"success": True, "message": "Connected to MongoDB"}
        except Exception as e:
            return {"success": False, "message": str(e)}
    
    @property
    def description(self) -> str:
        """Return description for LLM code generation."""
        return f"""
MongoDB document database at {self.host}:{self.port}/{self.database_name}

CRITICAL RULES:
1. Only use fields that EXIST in the schema - never assume fields like "email" exist
2. Use valid JSON: true/false/null (NOT Python True/False/None)
3. In $project, ALWAYS rename nested fields to simple aliases (no dots), then use those aliases in later stages

Use execute_query() with a JSON query string.

**Example - Query with nested fields (CORRECT pattern):**
```python
df = client.execute_query('''{{
    "collection": "users",
    "aggregate": [
        {{"$match": {{"status": "active"}}}},
        {{"$project": {{
            "userId": 1,
            "firstName": "$profile.name.first",
            "lastName": "$profile.name.last",
            "_id": 0
        }}}}
    ]
}}''')
```

**Example - Filter array then group:**
```python
df = client.execute_query('''{{
    "collection": "users",
    "aggregate": [
        {{"$unwind": "$orders"}},
        {{"$match": {{"orders.status": "completed"}}}},
        {{"$group": {{
            "_id": "$userId",
            "totalSpent": {{"$sum": "$orders.total"}}
        }}}},
        {{"$project": {{"userId": "$_id", "totalSpent": 1, "_id": 0}}}}
    ]
}}''')
```

**Basic find query:**
```python
df = client.execute_query('''{{
    "collection": "orders",
    "find": {{"status": "active"}},
    "limit": 100
}}''')
```

WRONG - Do NOT do this:
```
{{"$project": {{"profile.name.first": 1}}}},  // Creates literal field "profile.name.first"
{{"$group": {{"_id": "$profile.name.first"}}}}  // Tries nested path - FAILS!
```
"""

