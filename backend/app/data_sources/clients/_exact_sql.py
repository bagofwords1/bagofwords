"""Send a query to the database exactly as it was checked."""

import pandas as pd


def read_sql_exact(sql: str, conn) -> pd.DataFrame:
    """Run `sql` on the SQLAlchemy connection `conn` and return its rows.

    The driver receives `sql` byte for byte: no `text()` (which reads
    `:name` as a bind marker, also inside a literal) and no parameters
    (which would make the driver read `%` as a placeholder). The read-only
    guard checked this exact text, so nothing may rewrite it on the way.
    `read_sql_query` rather than `read_sql`, which first looks the text up
    as a table name.
    """
    return pd.read_sql_query(sql, conn.execution_options(no_parameters=True))
