IF DB_ID('salesdb') IS NOT NULL BEGIN ALTER DATABASE salesdb SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE salesdb; END
GO
CREATE DATABASE salesdb;
GO
USE salesdb;
GO
CREATE TABLE dbo.customers (customer_id int PRIMARY KEY, name nvarchar(100) NOT NULL, is_test bit NOT NULL DEFAULT 0, region nvarchar(10) NOT NULL);
CREATE TABLE dbo.orders (order_id int PRIMARY KEY, customer_id int NOT NULL REFERENCES dbo.customers(customer_id), order_date date NOT NULL, status nvarchar(20) NOT NULL, gross_amount decimal(12,2) NOT NULL, discount_amount decimal(12,2) NOT NULL DEFAULT 0);
CREATE TABLE dbo.refunds (refund_id int PRIMARY KEY, order_id int NOT NULL REFERENCES dbo.orders(order_id), amount decimal(12,2) NOT NULL);
GO
INSERT INTO dbo.customers VALUES (1,'Acme',0,'EMEA'),(2,'Globex',0,'AMER'),(3,'QA Bot',1,'AMER'),(4,'Initech',0,'APAC');
INSERT INTO dbo.orders VALUES
 (101,1,'2026-02-10','complete',1000,100),(102,2,'2026-03-05','complete',2000,0),(103,3,'2026-03-07','complete',5000,0),
 (104,4,'2026-04-01','cancelled',3000,0),(105,2,'2026-04-15','complete',1500,50),(106,1,'2026-05-20','pending',700,0),
 (107,4,'2026-06-02','complete',1200,200);
INSERT INTO dbo.refunds VALUES (1,102,300),(2,107,100);
GO
-- Fiscal year starts on February 1st.
CREATE FUNCTION dbo.fn_fiscal_quarter(@d date) RETURNS varchar(12) AS
BEGIN
  DECLARE @s date = DATEADD(month, -1, @d);
  RETURN 'FY' + CAST(YEAR(@s) AS varchar(4)) + '-Q' + CAST(((MONTH(@s) - 1) / 3) + 1 AS varchar(1));
END
GO
CREATE PROCEDURE dbo.usp_load_net_revenue @from date, @to date AS
BEGIN
  -- Net revenue = gross - discount - refunds, COMPLETE orders only,
  -- excluding internal test customers (customers.is_test = 1).
  SELECT o.order_id, o.gross_amount - o.discount_amount - ISNULL(r.refunded, 0) AS net_revenue
  INTO #net_revenue
  FROM dbo.orders o
  JOIN dbo.customers c ON c.customer_id = o.customer_id
  LEFT JOIN (SELECT order_id, SUM(amount) AS refunded FROM dbo.refunds GROUP BY order_id) r ON r.order_id = o.order_id
  WHERE o.status = 'complete' AND c.is_test = 0 AND o.order_date BETWEEN @from AND @to;
END
GO
EXEC sp_addextendedproperty 'MS_Description', 'Canonical net revenue calculation', 'SCHEMA', 'dbo', 'PROCEDURE', 'usp_load_net_revenue';
GO
-- Awkward identifier: a space in the name (review finding #7).
CREATE PROCEDURE dbo.[usp archive orders] @before date AS
BEGIN
  SELECT order_id FROM dbo.orders WHERE order_date < @before;
END
GO
-- A login that can read data but NOT view routine definitions.
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'reader') CREATE LOGIN reader WITH PASSWORD = 'Reader!Pass123', CHECK_POLICY = OFF;
GO
CREATE USER reader FOR LOGIN reader;
ALTER ROLE db_datareader ADD MEMBER reader;
GRANT EXECUTE ON dbo.fn_fiscal_quarter TO reader;
GO
