"""Run against dedicated, pre-seeded databases using two authenticated reader identities."""
import json
import os

import pytest

from app.data_sources.fabric_company_rls import CompanyPolicy
from app.data_sources.fast.rls import Identity


@pytest.mark.parametrize("kind", ["postgresql", "mssql", "oracledb"])
def test_native_reader_projection_and_row_scope(kind):
    config = os.getenv(f"BOW_ACCESS_{kind.upper()}")
    if not config:
        pytest.skip(f"Requires dedicated {kind} sales fixture")
    from app.schemas.data_source_registry import resolve_client_class
    credentials = json.loads(config)
    table = os.environ[f"BOW_ACCESS_{kind.upper()}_TABLE"]
    column = str.upper if kind == "oracledb" else str.lower
    company, amount = column("company"), column("amount")
    rule = dict(table=table, company_column=company, row_attribute="custom.companies",
                columns={company: None, amount: {"source": "roles", "values": ["finance"]}})
    policy = CompanyPolicy.model_validate(dict(tables=[rule]))
    for employee, companies, roles, expected in [
        ("one@example.com", ["101"], set(), [("101",)]),
        ("two@example.com", ["202"], {"finance"}, [("202", 90)]),
        ("three@example.com", [], set(), []),
    ]:
        client = resolve_client_class("MSSQL" if kind == "mssql" else kind)(**credentials)
        client.validate_company_policy(policy)
        identity = Identity(user_id=employee, email=employee, role_names=roles,
                            custom_attributes={"companies": companies})
        client.bind_company_reader(policy, employee, identity)
        frame = client.execute_query(f"SELECT * FROM {table}")
        assert list(frame.itertuples(index=False, name=None)) == expected
        if not roles:
            with pytest.raises(ValueError):
                client.execute_query(f"SELECT {amount} FROM {table}")


@pytest.mark.parametrize("kind", ["postgresql", "mssql", "oracledb"])
def test_committed_entitlement_revocation(kind):
    config = os.getenv(f"BOW_ACCESS_{kind.upper()}")
    if not config or os.getenv("BOW_ACCESS_MUTABLE_FIXTURE") != "1":
        pytest.skip("Requires a disposable, writable entitlement fixture")
    from sqlalchemy import text
    from app.schemas.data_source_registry import resolve_client_class
    client_type = resolve_client_class("MSSQL" if kind == "mssql" else kind)
    cfg = json.loads(config)
    table = os.environ[f"BOW_ACCESS_{kind.upper()}_TABLE"]
    column = str.upper if kind == "oracledb" else str.lower
    grants = table.split(".")[0] + "." + column("bow_test_grants")
    company, amount = column("company"), column("amount")
    policy = CompanyPolicy.model_validate(dict(grants_table=grants, employee_column=column("employee"),
        company_column=company,source_column=column("source_name"),tables=[dict(table=table,company_column=company,
        source="sales",columns={company:None,amount:["two@example.com"]})]))
    operator=client_type(**cfg)
    one,two=(client_type(**cfg) for _ in range(2))
    for client, viewer in ((one,"one@example.com"),(two,"two@example.com")):
        client.validate_company_policy(policy)
        client.bind_company_reader(policy,viewer)
    query=f"SELECT * FROM {table}"
    assert one.execute_query(query).values.tolist()==[["101"]]
    assert two.execute_query(query).values.tolist()==[["202",90]]
    with operator.connect() as conn:
        conn.execute(text(f"DELETE FROM {grants} WHERE employee=:viewer"),{"viewer":"one@example.com"})
        conn.commit()
    try:
        assert one.execute_query(query).empty
        assert not one.has_company_grant(policy.tables[0])
        assert two.has_company_grant(policy.tables[0])
        assert two.execute_query(query).values.tolist()==[["202",90]]
    finally:
        with operator.connect() as conn:
            conn.execute(text(f"INSERT INTO {grants} VALUES (:viewer, '101', 'sales')"), {"viewer":"one@example.com"})
            conn.commit()
