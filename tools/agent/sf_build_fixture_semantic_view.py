"""Build a complex Snowflake semantic view for connector validation.

Mirrors the shape used for the Power BI fixtures so the two connectors are
exercised against comparable models:

  - FIVE logical tables (two facts over three dimensions), one CONFORMED
    dimension joined by both facts
  - a SNOWFLAKE chain (shipment -> product -> category), so a category-level
    question requires traversing a relationship that is not on the fact table
  - relationships declared with explicit primary/foreign keys
  - dimensions and metrics spanning several base tables

A semantic view expresses all of this INSIDE one object: `DESC SEMANTIC VIEW`
reports TABLE, RELATIONSHIP, DIMENSION and METRIC rows, and a connector that
reads only DIMENSION/METRIC loses both the base-table mapping and every join.

Secrets via env only: SF_ACCOUNT / SF_USER / SF_KEY_PEM / SF_KEY_PASS.
Idempotent: DDL is CREATE OR REPLACE throughout.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend"))

from sqlalchemy import text  # noqa: E402
from app.data_sources.clients.snowflake_client import SnowflakeClient  # noqa: E402

DATABASE = os.environ.get("SF_FIXTURE_DB", "DEMO_SEMANTIC_DB")
SCHEMA = os.environ.get("SF_FIXTURE_SCHEMA", "BOW_FIXTURES")

BASE_TABLES = [
    ("dim_depot", """
        depot_key      INT PRIMARY KEY,
        depot_name     VARCHAR(50),
        region         VARCHAR(50)
    """, [
        "(1, 'Northgate', 'North')",
        "(2, 'Southfield', 'South')",
    ]),
    ("dim_category", """
        category_key   INT PRIMARY KEY,
        category_name  VARCHAR(50)
    """, [
        "(10, 'Drivetrain')",
        "(20, 'Consumables')",
    ]),
    ("dim_part", """
        part_key       INT PRIMARY KEY,
        part_name      VARCHAR(50),
        category_key   INT
    """, [
        "(100, 'Gearbox Seal', 10)",
        "(101, 'Clutch Plate', 10)",
        "(102, 'Oil Filter', 20)",
    ]),
    # Northgate parts_cost = 1200 + 800 = 2000 ; Southfield = 300 + 1500 = 1800
    ("fact_service_event", """
        event_key      INT PRIMARY KEY,
        depot_key      INT,
        part_key       INT,
        labour_hours   FLOAT,
        parts_cost     FLOAT
    """, [
        "(1, 1, 100, 4.0, 1200.0)",
        "(2, 1, 102, 2.5, 800.0)",
        "(3, 2, 101, 1.0, 300.0)",
        "(4, 2, 100, 6.0, 1500.0)",
    ]),
    # Conformed dimension: fact_part_shipment also joins dim_depot.
    # Drivetrain freight = 500 + 250 = 750 ; Consumables = 120
    ("fact_part_shipment", """
        shipment_key   INT PRIMARY KEY,
        depot_key      INT,
        part_key       INT,
        units          INT,
        freight_cost   FLOAT
    """, [
        "(1, 1, 100, 1000, 500.0)",
        "(2, 2, 101, 400, 250.0)",
        "(3, 1, 102, 250, 120.0)",
    ]),
]

SEMANTIC_VIEW = f"""
CREATE OR REPLACE SEMANTIC VIEW {DATABASE}.{SCHEMA}.FLEET_OPS_SV
    TABLES (
        depots      AS {DATABASE}.{SCHEMA}.dim_depot          PRIMARY KEY (depot_key),
        categories  AS {DATABASE}.{SCHEMA}.dim_category       PRIMARY KEY (category_key),
        parts       AS {DATABASE}.{SCHEMA}.dim_part           PRIMARY KEY (part_key),
        services    AS {DATABASE}.{SCHEMA}.fact_service_event PRIMARY KEY (event_key),
        shipments   AS {DATABASE}.{SCHEMA}.fact_part_shipment PRIMARY KEY (shipment_key)
    )
    RELATIONSHIPS (
        services_to_depot     AS services  (depot_key)    REFERENCES depots,
        services_to_part      AS services  (part_key)     REFERENCES parts,
        shipments_to_depot    AS shipments (depot_key)    REFERENCES depots,
        shipments_to_part     AS shipments (part_key)     REFERENCES parts,
        part_to_category      AS parts     (category_key) REFERENCES categories
    )
    FACTS (
        services.labour_hours AS labour_hours,
        services.parts_cost   AS parts_cost,
        shipments.units       AS units,
        shipments.freight_cost AS freight_cost
    )
    DIMENSIONS (
        depots.depot_name        AS depot_name,
        depots.region            AS region,
        categories.category_name AS category_name,
        parts.part_name          AS part_name
    )
    METRICS (
        services.total_parts_cost   AS SUM(services.parts_cost),
        services.total_labour_hours AS SUM(services.labour_hours),
        services.service_count      AS COUNT(services.event_key),
        shipments.total_freight     AS SUM(shipments.freight_cost),
        shipments.total_units       AS SUM(shipments.units)
    )
"""


def main():
    client = SnowflakeClient(
        account=os.environ["SF_ACCOUNT"],
        warehouse=os.environ.get("SF_WAREHOUSE", "COMPUTE_WH"),
        database=DATABASE,
        schema=None,
        user=os.environ["SF_USER"],
        private_key_pem=os.environ["SF_KEY_PEM"],
        private_key_passphrase=os.environ.get("SF_KEY_PASS"),
    )
    with client.connect() as conn:
        conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {DATABASE}"))
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {DATABASE}.{SCHEMA}"))
        print(f"schema {DATABASE}.{SCHEMA} ready")

        for name, cols, rows in BASE_TABLES:
            fqn = f"{DATABASE}.{SCHEMA}.{name}"
            conn.execute(text(f"CREATE OR REPLACE TABLE {fqn} ({cols})"))
            conn.execute(text(f"INSERT INTO {fqn} VALUES {', '.join(rows)}"))
            n = conn.execute(text(f"SELECT COUNT(*) FROM {fqn}")).scalar()
            print(f"  {name:20s} {n} rows")

        conn.execute(text(SEMANTIC_VIEW))
        print(f"\nsemantic view {DATABASE}.{SCHEMA}.FLEET_OPS_SV created")

        rows = conn.execute(text(f"DESC SEMANTIC VIEW {DATABASE}.{SCHEMA}.FLEET_OPS_SV")).fetchall()
        kinds = {}
        for r in rows:
            kinds[r[0]] = kinds.get(r[0], 0) + 1
        print(f"  DESC rows: {len(rows)}  by kind: {kinds}")

        # Prove the model answers the questions the e2e will ask.
        for label, q in [
            ("parts cost by depot",
             f"SELECT * FROM SEMANTIC_VIEW({DATABASE}.{SCHEMA}.FLEET_OPS_SV "
             f"DIMENSIONS depot_name METRICS total_parts_cost)"),
            ("freight by category (snowflake hop)",
             f"SELECT * FROM SEMANTIC_VIEW({DATABASE}.{SCHEMA}.FLEET_OPS_SV "
             f"DIMENSIONS category_name METRICS total_freight)"),
        ]:
            try:
                res = conn.execute(text(q)).fetchall()
                print(f"  {label}: {[tuple(r) for r in res]}")
            except Exception as e:
                print(f"  {label}: FAILED {str(e)[:220]}")


if __name__ == "__main__":
    main()
