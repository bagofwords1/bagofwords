from typing import List, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.datasource_table import DataSourceTable
import re


_TRIPLE_REF = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
_DOUBLE_REF = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")

_SQL_FUNCS = {
    "sum", "avg", "min", "max", "count", "concat",
    "lower", "upper", "coalesce", "substring", "trim",
    "case", "when", "then", "else", "end"
}

def _extract_table_column_refs(source: str) -> List[Tuple[str, str]]:
    """Extract (table, column) pairs from a SQL-like expression.
    - Prefer triples schema.table.column and derive (table, column)
    - For doubles table.column, include only when the right token is not also used
      as a table in any triple (avoids capturing schema.table as (schema, table)).
    - Skip SQL function names as potential tables.
    """
    if not source or not isinstance(source, str):
        return []

    pairs: List[Tuple[str, str]] = []

    triple_tables: set[str] = set()
    for m in _TRIPLE_REF.finditer(source):
        table, column = m.group(2), m.group(3)
        triple_tables.add(table)
        pair = (table, column)
        if pair not in pairs:
            pairs.append(pair)

    for m in _DOUBLE_REF.finditer(source):
        left, right = m.group(1), m.group(2)
        # Skip obvious function calls
        if left.lower() in _SQL_FUNCS:
            continue
        # If the right token is known to be a table from any triple in this source,
        # then this double is likely schema.table and should be ignored.
        if right in triple_tables:
            continue
        pair = (left, right)
        if pair not in pairs:
            pairs.append(pair)

    return pairs


async def extract_tables_from_data_model(
    db: AsyncSession,
    data_model: Dict[str, Any],
    data_source_ids: List[str]
) -> List[Dict[str, Any]]:
    tables: Dict[Tuple[str | None, str], Dict[str, Any]] = {}
    if not data_model or not isinstance(data_model, dict):
        return []

    cols = data_model.get("columns", []) or []
    for col in cols:
        source = col.get("source") if isinstance(col, dict) else None
        explicit_ds_id = col.get("source_data_source_id") if isinstance(col, dict) else None
        for table_name, column_name in _extract_table_column_refs(source or ""):
            key = (explicit_ds_id, table_name.lower())
            if key not in tables:
                tables[key] = {
                    "table_name": table_name,
                    "columns": set(),
                    "datasource_table_id": None,
                    "datasource_id": explicit_ds_id,
                }
            if column_name:
                tables[key]["columns"].add(column_name)

    if not tables:
        return []

    resolved: List[Dict[str, Any]] = []
    report_ds_ids = list(dict.fromkeys([ds for ds in data_source_ids if ds]))

    for (explicit_ds_id, table_key), entry in tables.items():
        table_name = entry["table_name"].lower()
        ds_table_id = None
        ds_id = explicit_ds_id

        # Fallback to single report data source if no per-column id was provided
        if ds_id is None and len(report_ds_ids) == 1:
            ds_id = report_ds_ids[0]

        # Resolve datasource_table_id only if we have a ds_id
        if ds_id:
            stmt = select(DataSourceTable).where(
                DataSourceTable.datasource_id == ds_id,
                DataSourceTable.name == table_name
            )
            res = await db.execute(stmt)
            row = res.scalar_one_or_none()
            if row:
                ds_table_id = str(row.id)
            else:
                # If we can’t resolve the table in the known data source, skip this entry
                # to avoid polluting stats with schema tokens or unknown tables.
                continue

        resolved.append({
            "table_name": table_name,
            "columns": sorted(list(entry["columns"])),
            "datasource_table_id": ds_table_id,
            "datasource_id": ds_id,
        })

    return resolved

# Example data (for documentation/reference only); do not execute at import time.
# example_data_model = [
#     {"columns": [
#         {"generated_column_name": "customer_id", "source": "dvdrental.customer.customer_id"},
#         {"generated_column_name": "first_name", "source": "dvdrental.customer.first_name"},
#         {"generated_column_name": "last_name", "source": "dvdrental.customer.last_name"},
#         {"generated_column_name": "email", "source": "dvdrental.customer.email"},
#     ], "type": "table"},
#     {"columns": [
#         {"generated_column_name": "customer_name", "source": "dvdrental.customer.first_name || ' ' || dvdrental.customer.last_name"},
#         {"generated_column_name": "deal_status", "source": "CASE WHEN dvdrental.rental.return_date IS NULL THEN 'Open' ELSE 'Closed' END"},
#         {"generated_column_name": "amount", "source": "dvdrental.payment.amount"},
#     ], "type": "table"},
#     {"columns": [
#         {"generated_column_name": "recognized_revenue", "source": "SUM(dvdrental.payment.amount)"}
#     ], "type": "count"},
# ]
# ]