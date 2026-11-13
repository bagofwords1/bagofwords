"""Legitimate queries: what agents, saved steps and custom tables send.

Every entry must pass the read-only guard and table scope (every table
active) for its dialect. A refusal here is an over-refusal: a change that
refuses one of these breaks real use. The shapes come from the guard tests'
read fixtures, the custom-table (FAST) paths, the connectors' relative-date
hints and the identity-parameter tests.
"""

from __future__ import annotations

BS = chr(92)
NL = chr(10)
# Unquoted names outside ASCII (Hebrew: "name", "customers").
HE_NAME = chr(0x5E9) + chr(0x5DD)
HE_TABLE = chr(0x5DC) + chr(0x5E7) + chr(0x5D5) + chr(0x5D7) + chr(0x5D5) + chr(0x5EA)

# The tables the corpus reads; a scope with all of them active.
CORPUS_TABLES: list[str] = [
    "public.orders", "public.customers", "public.order_items", "public.products", "public.events",
    "public.users", "public.t", "public.u", "public.items", "public.Order Details",
    "sales.invoices", "sales.regions",
    "information_schema.tables", "information_schema.columns", "pg_catalog.pg_stat_activity",
    "analytics.daily_sales", "proj.dataset.orders", "dbo.orders", "dbo.customers", "dbo.users", "main.orders",
    "public." + HE_TABLE,
]

_POSTGRES = [
    # Read fixtures shared by every dialect.
    "SELECT 1",
    "SELECT id, amount FROM orders WHERE status = 'deleted' ORDER BY amount DESC",
    "SELECT REPLACE(name, 'a', 'b') AS n FROM customers",
    "WITH big AS (SELECT * FROM orders WHERE amount > 100) SELECT COUNT(*) FROM big",
    "SELECT a FROM t UNION ALL SELECT b FROM u",
    "SELECT id, ROW_NUMBER() OVER (PARTITION BY c ORDER BY d) AS rn FROM t",
    "-- update the totals" + NL + "SELECT SUM(amount) FROM orders /* drop me */",
    "SELECT * FROM orders WHERE note = 'please DELETE FROM my list';",
    "SELECT * FROM orders LIMIT 5; -- sample",
    "SELECT * FROM orders;" + NL + "-- trailing note",
    # Dates: literals the checks must not blank or rewrite.
    "SELECT date_part('year', created_at) AS y, COUNT(*) FROM orders GROUP BY 1 ORDER BY 1",
    "SELECT date_trunc('month', created_at) AS m, SUM(amount) FROM orders "
    "WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' GROUP BY 1",
    "SELECT EXTRACT(YEAR FROM created_at) AS y FROM orders",
    "SELECT to_char(created_at, 'YYYY-MM') AS ym, COUNT(*) FROM orders GROUP BY 1",
    "SELECT * FROM orders WHERE created_at BETWEEN DATE '2026-01-01' AND TIMESTAMP '2026-03-31 23:59:59'",
    "SELECT * FROM events WHERE ts > now() - interval '1 hour'",
    "SELECT created_at::date AS d, COUNT(*) FROM orders GROUP BY created_at::date",
    # Literals in every form the server reads.
    "SELECT * FROM users WHERE login = 'DOMAIN" + BS + "user'",
    "SELECT * FROM orders WHERE code ~ '^" + BS + "d{3}-" + BS + "d+$'",
    "SELECT regexp_replace(name, '" + BS + "s+', ' ', 'g') AS n FROM customers",
    "SELECT * FROM customers WHERE name = 'O''Brien'",
    "SELECT E'line one" + BS + "nline two' AS note FROM orders LIMIT 1",
    "SELECT E'it" + BS + "'s' AS v",
    "SELECT $$it's a note$$ AS v, $tag$a;b$tag$ AS w FROM orders",
    "SELECT * FROM products WHERE name ILIKE '%chair%'",
    "SELECT * FROM products WHERE sku LIKE 'A" + BS + "_%'",
    "SELECT N'text' AS v, B'0101' AS bits, X'1F' AS hex",
    "SELECT ':name' AS a, '%' AS b, 'a%%b' AS c FROM orders",
    "SELECT 'a' || 'b' AS ab, concat(first_name, ' ', last_name) AS full_name FROM customers",
    "SELECT * FROM orders WHERE note = '-- not a comment' AND tag = '/* nor this */'",
    "SELECT '{\"a\": 1}'::jsonb ->> 'a' AS a",
    "SELECT data ->> 'name' AS n, data -> 'tags' AS t FROM events WHERE data ->> 'kind' = 'click'",
    "SELECT * FROM events WHERE data @> '{\"kind\": \"click\"}'",
    # Names.
    'SELECT "Order Date", "Customer Name" FROM "Order Details"',
    'SELECT o."Amount" FROM public.orders AS o',
    "SELECT o.id, c.name FROM public.orders o JOIN public.customers c ON c.id = o.customer_id",
    "SELECT i.* FROM sales.invoices i LEFT JOIN sales.regions r ON r.id = i.region_id WHERE r.id IS NULL",
    "SELECT " + HE_NAME + ", COUNT(*) FROM " + HE_TABLE + " GROUP BY " + HE_NAME,
    'SELECT "' + HE_NAME + '" FROM public."' + HE_TABLE + '"',
    # Query shapes.
    "SELECT status, COUNT(*) FILTER (WHERE amount > 100) AS big FROM orders GROUP BY status",
    "SELECT DISTINCT ON (customer_id) customer_id, created_at FROM orders ORDER BY customer_id, created_at DESC",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) AS median FROM orders",
    "SELECT string_agg(name, ', ' ORDER BY name) AS names, array_agg(id) AS ids FROM customers",
    "SELECT * FROM orders ORDER BY amount DESC NULLS LAST LIMIT 10 OFFSET 20",
    "SELECT * FROM orders ORDER BY id FETCH FIRST 5 ROWS ONLY",
    "SELECT d::date FROM generate_series(DATE '2026-01-01', DATE '2026-01-31', INTERVAL '1 day') AS g(d)",
    "SELECT c.id, x.n FROM customers c CROSS JOIN LATERAL "
    "(SELECT COUNT(*) AS n FROM orders o WHERE o.customer_id = c.id) x",
    "SELECT CASE WHEN amount > 100 THEN 'big' WHEN amount > 10 THEN 'mid' ELSE 'small' END AS size FROM orders",
    "SELECT COALESCE(NULLIF(trim(name), ''), 'unknown') AS n FROM customers",
    "SELECT * FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE region = 'EU')",
    "SELECT * FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id)",
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 10) SELECT i FROM n",
    "SELECT amount, SUM(amount) OVER (ORDER BY created_at ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) FROM orders",
    "SELECT * FROM orders WHERE id = ANY (ARRAY[1, 2, 3])",
    "SELECT round(avg(amount)::numeric, 2) AS avg_amount FROM orders",
    "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS v(id, label)",
    "(SELECT 1)",
    "SELECT pg_advisory_xact_lock(1)",
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'orders'",
    "SELECT pid, state, query_start FROM pg_catalog.pg_stat_activity WHERE state <> 'idle'",
    # Custom tables (FAST): the windowed and incremental reads a refresh sends.
    "SELECT * FROM orders WHERE updated_at >= '2026-01-01 00:00:00' AND updated_at < '2026-01-02 00:00:00'",
    "SELECT * FROM (SELECT id, amount FROM orders) bow_sub LIMIT 100",
    "SELECT id, updated_at, E'it" + BS + "'s' AS lit /* note */ FROM items",
    "SELECT COUNT(*) FROM (SELECT * FROM orders WHERE seq >= 200) bow_count",
    "/*+ SeqScan(orders) */ SELECT COUNT(*) FROM orders",
]

_OTHER_DIALECTS = {
    "redshift": [
        "SELECT DATEADD(day, -7, GETDATE()) AS since, COUNT(*) FROM orders",
        "SELECT date_trunc('month', created_at) AS m, SUM(amount) FROM orders GROUP BY 1",
        "SELECT * FROM orders WHERE status = 'it''s fine' LIMIT 10",
        "SELECT listagg(name, ', ') WITHIN GROUP (ORDER BY name) FROM customers",
    ],
    "mysql": [
        "SELECT `id`, `amount` FROM `orders` WHERE `status` = 'open' LIMIT 10",
        "SELECT DATE_FORMAT(created_at, '%Y-%m') AS ym, COUNT(*) FROM orders GROUP BY ym",
        "SELECT * FROM orders WHERE created_at >= NOW() - INTERVAL 7 DAY",
        "SELECT * FROM customers WHERE name = 'O''Brien'",
        "SELECT GROUP_CONCAT(name SEPARATOR ', ') FROM customers",
        "SELECT IFNULL(amount, 0) AS a FROM orders -- note",
        "SELECT * FROM orders WHERE note LIKE '%" + BS + BS + "%'",
    ],
    "tsql": [
        "SELECT TOP 10 [id], [amount] FROM [dbo].[orders] ORDER BY [amount] DESC",
        "SELECT DATEPART(year, created_at) AS y, COUNT(*) FROM dbo.orders GROUP BY DATEPART(year, created_at)",
        "SELECT * FROM dbo.customers WHERE name = N'Zo" + chr(0xEB) + "'",
        "SELECT * FROM dbo.orders WHERE created_at >= DATEADD(day, -7, GETDATE())",
        "SELECT * FROM dbo.users WHERE login = 'DOMAIN" + BS + "user'",
        "WITH t AS (SELECT * FROM dbo.orders) SELECT COUNT(*) FROM t",
        "SELECT * FROM dbo.orders ORDER BY id OFFSET 10 ROWS FETCH NEXT 10 ROWS ONLY",
    ],
    "oracle": [
        "SELECT * FROM orders FETCH FIRST 10 ROWS ONLY",
        "SELECT TO_CHAR(created_at, 'YYYY-MM') AS ym, COUNT(*) FROM orders GROUP BY TO_CHAR(created_at, 'YYYY-MM')",
        "SELECT * FROM orders WHERE ROWNUM <= 10",
        "SELECT NVL(amount, 0) FROM orders WHERE created_at >= SYSDATE - 7",
        "SELECT * FROM customers WHERE name = 'O''Brien'",
        "SELECT id FROM orders WHERE status = 'open' /* note */",
    ],
    "sqlite": [
        "SELECT strftime('%Y', created_at) AS y, COUNT(*) FROM orders GROUP BY y",
        "SELECT * FROM orders WHERE created_at >= date('now', '-7 days')",
        "SELECT * FROM customers WHERE name = 'O''Brien'",
        "SELECT * FROM users WHERE login = 'DOMAIN" + BS + "user'",
        'SELECT "Order Date" FROM "Order Details"',
    ],
    "snowflake": [
        "SELECT DATE_TRUNC('month', created_at) AS m, SUM(amount) FROM orders GROUP BY 1",
        "SELECT * FROM orders WHERE created_at >= DATEADD(day, -7, CURRENT_DATE())",
        "SELECT * FROM orders QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at DESC) = 1",
        "SELECT * FROM products WHERE name ILIKE '%chair%'",
        "SELECT data:name::string AS n FROM events",
        "SELECT * FROM users WHERE login = 'DOMAIN" + BS + BS + "user'",
    ],
    "bigquery": [
        "SELECT * FROM `proj.dataset.orders` WHERE status = 'open' LIMIT 10",
        "SELECT EXTRACT(YEAR FROM created_at) AS y, COUNT(*) FROM `proj.dataset.orders` GROUP BY y",
        "SELECT SAFE_CAST(amount AS NUMERIC) AS a FROM `proj.dataset.orders`",
        "SELECT * FROM `proj.dataset.orders` WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)",
    ],
    "athena": [
        "SELECT date_trunc('month', created_at) AS m, COUNT(*) FROM orders GROUP BY 1",
        "SELECT approx_distinct(customer_id) FROM orders WHERE created_at >= current_date - interval '7' day",
        "SELECT * FROM orders WHERE status = 'open' LIMIT 10",
    ],
    "trino": [
        "SELECT date_trunc('month', created_at) AS m, COUNT(*) FROM orders GROUP BY 1",
        "SELECT * FROM orders WHERE created_at >= current_date - interval '7' day",
        "SELECT approx_percentile(amount, 0.5) FROM orders",
    ],
    "clickhouse": [
        "SELECT toStartOfMonth(created_at) AS m, count() FROM orders GROUP BY m",
        "SELECT * FROM orders WHERE created_at >= now() - INTERVAL 7 DAY LIMIT 10",
        "SELECT uniqExact(customer_id) FROM orders",
    ],
    "databricks": [
        "SELECT date_format(created_at, 'yyyy-MM') AS ym, COUNT(*) FROM orders GROUP BY 1",
        "SELECT * FROM orders WHERE created_at >= date_sub(current_date(), 7)",
        "SELECT `id`, `amount` FROM orders LIMIT 10",
    ],
    "duckdb": [
        "SELECT date_trunc('month', created_at) AS m, SUM(amount) FROM orders GROUP BY 1",
        "SELECT * FROM orders WHERE created_at >= current_date - INTERVAL 7 DAY",
        "SELECT * FROM customers WHERE name = 'O''Brien'",
        "SELECT * FROM users WHERE login = 'DOMAIN" + BS + "user'",
    ],
}

# (dialect, sql)
LEGIT_QUERIES: list[tuple[str, str]] = [("postgres", sql) for sql in _POSTGRES] + [
    (dialect, sql) for dialect, queries in _OTHER_DIALECTS.items() for sql in queries
]

# Legitimate queries the checks refuse today, and why. Each is expected to
# fail until the rule is fixed; then it moves to LEGIT_QUERIES.
# (dialect, sql, reason)
KNOWN_OVER_REFUSALS: list[tuple[str, str, str]] = [
    ("postgres", "VALUES (1, 'a'), (2, 'b')",
     "a top-level VALUES parses as a Values statement, which the parsed path's read statements do not "
     "include (unchanged from before this PR; VALUES inside a FROM passes)"),
]

_IDENTITY_VALUES = ["sarah@example.com", "DOMAIN" + BS + "user", "it's", "o'neil" + BS + "x"]
# Engines whose renderer refuses a backslash in a value.
_NO_BACKSLASH = {"redshift", "athena"}
_IDENTITY_TEMPLATES = {
    "postgres": [
        "SELECT * FROM orders WHERE owner = :owner AND date_part('year', created_at) = 2026",
        "SELECT id, amount FROM orders WHERE owner = :owner -- the viewer's rows" + NL + "ORDER BY id",
        "WITH mine AS (SELECT * FROM orders WHERE owner = :owner) SELECT COUNT(*) FROM mine",
    ],
    "redshift": ["SELECT * FROM orders WHERE owner = :owner AND date_part('year', created_at) = 2026"],
    "mysql": ["SELECT * FROM orders WHERE owner = :owner AND YEAR(created_at) = 2026"],
    "tsql": ["SELECT * FROM dbo.orders WHERE owner = :owner AND DATEPART(year, created_at) = 2026"],
    "oracle": ["SELECT * FROM orders WHERE owner = :owner"],
    "sqlite": ["SELECT * FROM orders WHERE owner = :owner AND strftime('%Y', created_at) = '2026'"],
    "snowflake": ["SELECT * FROM orders WHERE owner = :owner AND date_part('year', created_at) = 2026"],
    "bigquery": ["SELECT * FROM `proj.dataset.orders` WHERE owner = :owner"],
    "athena": ["SELECT * FROM orders WHERE owner = :owner"],
    "trino": ["SELECT * FROM orders WHERE owner = :owner"],
    "clickhouse": ["SELECT * FROM orders WHERE owner = :owner"],
    "databricks": ["SELECT * FROM orders WHERE owner = :owner"],
    "duckdb": ["SELECT * FROM orders WHERE owner = :owner"],
}

# (dialect, sql with an `:owner` placeholder, the viewer's identity value)
IDENTITY_QUERIES: list[tuple[str, str, str]] = [
    (dialect, template, value)
    for dialect, templates in _IDENTITY_TEMPLATES.items()
    for template in templates
    for value in _IDENTITY_VALUES
    if not (dialect in _NO_BACKSLASH and BS in value)
]
