-- Postgres fixture for per-user cached copies (docs/feedback-loops/per-user-cached-copies.md).
--
-- One fact table protected by native row-level security, driven by a mapping
-- table, plus two logins that see different slices. A custom table extracted
-- with each login's own credentials must hold exactly that login's rows.
--
--   psql -v rows=2000000 -v pw="'...'" -f pg_rls_fixture.sql
--
-- demo1: company 1, departments 10 and 11
-- demo2: company 2, department 20
-- demo3: no mapping rows at all (must see nothing)
\set ON_ERROR_STOP on

DROP TABLE IF EXISTS sales, departments, bow_rls CASCADE;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo1') THEN CREATE ROLE demo1 LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo2') THEN CREATE ROLE demo2 LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo3') THEN CREATE ROLE demo3 LOGIN; END IF;
END $$;
ALTER ROLE demo1 PASSWORD :pw;
ALTER ROLE demo2 PASSWORD :pw;
ALTER ROLE demo3 PASSWORD :pw;

CREATE TABLE bow_rls (
  login         text    NOT NULL,
  company_id    integer NOT NULL,
  department_id integer NOT NULL,
  PRIMARY KEY (login, company_id, department_id)
);
INSERT INTO bow_rls VALUES ('demo1', 1, 10), ('demo1', 1, 11), ('demo2', 2, 20);

CREATE TABLE departments (
  id         integer PRIMARY KEY,
  company_id integer NOT NULL,
  name       text    NOT NULL
);
INSERT INTO departments VALUES
  (10, 1, 'Acme Sales'), (11, 1, 'Acme Support'), (12, 1, 'Acme Finance'),
  (20, 2, 'Globex Sales'), (21, 2, 'Globex Finance'),
  (30, 3, 'Initech Sales');

CREATE TABLE sales (
  id            bigint PRIMARY KEY,
  company_id    integer       NOT NULL,
  department_id integer       NOT NULL,
  customer      text          NOT NULL,
  amount        numeric(12,2) NOT NULL,
  sold_at       timestamp     NOT NULL
);
-- Deterministic: department cycles over the six above, so every slice is an
-- exact sixth of :rows and totals can be checked by formula.
INSERT INTO sales
SELECT g,
       (ARRAY[1,1,1,2,2,3])[1 + g % 6],
       (ARRAY[10,11,12,20,21,30])[1 + g % 6],
       'customer-' || (g % 5000),
       ((g % 1000) + 1)::numeric(12,2),
       timestamp '2025-01-01' + (g % 500) * interval '1 day'
FROM generate_series(1, :rows) AS g;
CREATE INDEX sales_slice ON sales (company_id, department_id);
ANALYZE sales;

ALTER TABLE sales ENABLE ROW LEVEL SECURITY;
ALTER TABLE departments ENABLE ROW LEVEL SECURITY;
CREATE POLICY sales_by_mapping ON sales FOR SELECT USING (
  EXISTS (SELECT 1 FROM bow_rls m
          WHERE m.login = current_user
            AND m.company_id = sales.company_id
            AND m.department_id = sales.department_id));
CREATE POLICY departments_by_mapping ON departments FOR SELECT USING (
  EXISTS (SELECT 1 FROM bow_rls m
          WHERE m.login = current_user
            AND m.company_id = departments.company_id
            AND m.department_id = departments.id));

GRANT SELECT ON sales, departments TO demo1, demo2, demo3;
-- The policies read the mapping as the querying role.
GRANT SELECT ON bow_rls TO demo1, demo2, demo3;
ALTER TABLE bow_rls ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_mapping ON bow_rls FOR SELECT USING (login = current_user);
