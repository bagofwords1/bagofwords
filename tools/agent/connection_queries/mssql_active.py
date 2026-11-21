"""Print how many of the loop's heavy SQL Server statements are running now.

Needs MSSQL_SA_PASSWORD (the throwaway container's sa password).
"""
import os

import pyodbc

conn = pyodbc.connect(
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=127.0.0.1,1433;UID=sa;"
    f"PWD={os.environ['MSSQL_SA_PASSWORD']};TrustServerCertificate=yes",
    autocommit=True,
    timeout=5,
)
print(conn.cursor().execute(
    "SELECT COUNT(*) FROM sys.dm_exec_requests r CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t "
    "WHERE r.session_id<>@@SPID AND t.text LIKE '%CROSS JOIN dbo.orders d%'"
).fetchone()[0])
