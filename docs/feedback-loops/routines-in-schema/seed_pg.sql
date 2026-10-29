DROP DATABASE IF EXISTS salesdb;
CREATE DATABASE salesdb;
\c salesdb

CREATE SCHEMA sales;

CREATE TABLE sales.customers (
  customer_id int PRIMARY KEY,
  name text NOT NULL,
  is_test boolean NOT NULL DEFAULT false,
  region text NOT NULL
);

CREATE TABLE sales.orders (
  order_id int PRIMARY KEY,
  customer_id int NOT NULL REFERENCES sales.customers(customer_id),
  order_date date NOT NULL,
  status text NOT NULL,           -- 'complete' | 'cancelled' | 'pending'
  gross_amount numeric(12,2) NOT NULL,
  discount_amount numeric(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE sales.refunds (
  refund_id int PRIMARY KEY,
  order_id int NOT NULL REFERENCES sales.orders(order_id),
  amount numeric(12,2) NOT NULL
);

INSERT INTO sales.customers VALUES
 (1,'Acme',false,'EMEA'),(2,'Globex',false,'AMER'),(3,'QA Bot',true,'AMER'),(4,'Initech',false,'APAC');

INSERT INTO sales.orders VALUES
 (101,1,'2026-02-10','complete',1000,100),
 (102,2,'2026-03-05','complete',2000,0),
 (103,3,'2026-03-07','complete',5000,0),     -- test customer: excluded
 (104,4,'2026-04-01','cancelled',3000,0),    -- cancelled: excluded
 (105,2,'2026-04-15','complete',1500,50),
 (106,1,'2026-05-20','pending',700,0),       -- pending: excluded
 (107,4,'2026-06-02','complete',1200,200);

INSERT INTO sales.refunds VALUES (1,102,300),(2,107,100);

-- Fiscal year starts on February 1st: Feb-Apr = Q1, May-Jul = Q2, ...
CREATE FUNCTION sales.fn_fiscal_quarter(d date) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT 'FY' || extract(year FROM (d - interval '1 month'))::int || '-Q' ||
         (floor((extract(month FROM (d - interval '1 month')) - 1) / 3) + 1)::int
$$;

-- Overload: fiscal quarter of a timestamp
CREATE FUNCTION sales.fn_fiscal_quarter(ts timestamp) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT sales.fn_fiscal_quarter(ts::date) $$;

-- The business definition of net revenue lives here.
CREATE PROCEDURE sales.usp_refresh_net_revenue(p_from date, p_to date)
LANGUAGE plpgsql AS $$
BEGIN
  -- Net revenue = gross - discount - refunds, for COMPLETE orders only,
  -- excluding internal test customers (customers.is_test).
  CREATE TEMP TABLE IF NOT EXISTS net_revenue_tmp AS
  SELECT o.order_id,
         o.gross_amount - o.discount_amount - coalesce(r.refunded, 0) AS net_revenue
  FROM sales.orders o
  JOIN sales.customers c ON c.customer_id = o.customer_id
  LEFT JOIN (SELECT order_id, sum(amount) AS refunded FROM sales.refunds GROUP BY order_id) r
         ON r.order_id = o.order_id
  WHERE o.status = 'complete'
    AND NOT c.is_test
    AND o.order_date BETWEEN p_from AND p_to;
END;
$$;

COMMENT ON PROCEDURE sales.usp_refresh_net_revenue(date, date) IS 'Canonical net revenue calculation';

-- A function sharing its name with a table (legal in Postgres).
CREATE FUNCTION sales.orders(p_customer int) RETURNS SETOF sales.orders
LANGUAGE sql STABLE AS $$ SELECT * FROM sales.orders WHERE customer_id = p_customer $$;
