-- Synthetic retail source for the cached-table column-metadata loop
-- (docs/feedback-loops/cached-table-column-metadata.md).
--
-- Deterministic: no random(), so every run produces the same figures. The
-- point of the data is that a customer buys in several months and regions, so
-- per-row distinct customer counts do NOT add up to the distinct count of a
-- coarser grain. Every column carries a comment, which is what indexing
-- stores and a cached table later inherits.
--
--   createdb retail && psql -d retail -f tools/agent/cached_table_metadata_seed.sql

DROP TABLE IF EXISTS orders, customers, stores;

CREATE TABLE stores (
    store_id   integer PRIMARY KEY,
    region     text NOT NULL,
    store_type text NOT NULL
);
COMMENT ON TABLE stores IS 'One row per physical or online store.';
COMMENT ON COLUMN stores.store_id IS 'Store identifier.';
COMMENT ON COLUMN stores.region IS 'Sales region the store reports to (North, South, East, West).';
COMMENT ON COLUMN stores.store_type IS 'Store format: Flagship, Outlet or Online.';

CREATE TABLE customers (
    customer_id integer PRIMARY KEY,
    segment     text NOT NULL
);
COMMENT ON TABLE customers IS 'One row per registered customer.';
COMMENT ON COLUMN customers.customer_id IS 'Customer identifier.';
COMMENT ON COLUMN customers.segment IS 'Marketing segment: Consumer, Small Business or Enterprise.';

CREATE TABLE orders (
    order_id    integer PRIMARY KEY,
    store_id    integer NOT NULL REFERENCES stores (store_id),
    customer_id integer NOT NULL REFERENCES customers (customer_id),
    order_date  date NOT NULL,
    amount      numeric(12, 2) NOT NULL,
    quantity    integer NOT NULL
);
COMMENT ON TABLE orders IS 'One row per completed order.';
COMMENT ON COLUMN orders.order_id IS 'Order identifier.';
COMMENT ON COLUMN orders.store_id IS 'Store that took the order.';
COMMENT ON COLUMN orders.customer_id IS 'Customer who placed the order.';
COMMENT ON COLUMN orders.order_date IS 'Calendar date the order was completed.';
COMMENT ON COLUMN orders.amount IS 'Order value in USD, net of tax and discounts.';
COMMENT ON COLUMN orders.quantity IS 'Number of units in the order.';

INSERT INTO stores
SELECT s,
       (ARRAY['North', 'South', 'East', 'West'])[1 + (s % 4)],
       (ARRAY['Flagship', 'Outlet', 'Online'])[1 + (s % 3)]
FROM generate_series(1, 24) AS s;

INSERT INTO customers
SELECT c, (ARRAY['Consumer', 'Small Business', 'Enterprise'])[1 + (c % 3)]
FROM generate_series(1, 600) AS c;

-- 24,000 orders over 2025. Customer and store are picked by different
-- multipliers so each customer spreads over many stores, regions and months.
INSERT INTO orders
SELECT o,
       1 + ((o * 7) % 24),
       1 + ((o * 13 + (o / 97)) % 600),
       DATE '2025-01-01' + ((o * 11) % 365),
       round((20 + ((o * 37) % 480) + ((o % 9) * 0.25))::numeric, 2),
       1 + (o % 5)
FROM generate_series(1, 24000) AS o;

ANALYZE;
