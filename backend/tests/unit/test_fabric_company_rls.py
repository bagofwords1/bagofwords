"""Execute the generated query on two disjoint companies with live grant changes."""
import sqlite3

import pytest

from app.data_sources.fabric_company_rls import CompanyPolicy, protect_query


@pytest.fixture
def policy():
    return CompanyPolicy.model_validate({
        "grants_table": "demo.access_map",
        "employee_column": "employee", "company_column": "company",
        "source_column": "source",
        "tables": [
            {"table": "demo.sales", "company_column": "company", "source": "sales"},
            {"table": "demo.costs", "company_column": "company", "source": "costs"},
        ],
    })


def test_fresh_grants_on_every_read(policy):
    with sqlite3.connect(":memory:") as db:
        db.create_function("CONCAT", 2, lambda a, b: str(a if a is not None else "") + b)
        db.create_collation("Latin1_General_100_BIN2_UTF8", lambda a, b: (a > b) - (a < b))
        db.executescript("""
            ATTACH ':memory:' AS demo;
            CREATE TABLE demo.sales(company INTEGER, amount INTEGER);
            CREATE TABLE demo.costs(company INTEGER, amount INTEGER);
            CREATE TABLE demo.access_map(employee TEXT, company INTEGER, source TEXT);
            INSERT INTO demo.sales VALUES (101,10),(202,90);
            INSERT INTO demo.costs SELECT * FROM demo.sales;
            INSERT INTO demo.access_map VALUES ('one',101,'sales'),('one',101,'sales'),
                ('two',202,'sales'),('one',202,'costs'),('one ',202,'sales'),('one',202,'sales ');
        """)
        def read(sql, viewer):
            statement, params = protect_query(sql, policy, viewer)
            return db.execute(statement, params).fetchall()

        assert read("SELECT company FROM demo.sales", "one") == [(101,)]
        assert read("SELECT company FROM demo.sales", "two") == [(202,)]
        assert read("SELECT company FROM demo.sales WHERE company=202", "one") == []
        assert read("SELECT SUM(amount) FROM demo.sales", "one") == [(10,)]
        assert read("SELECT a.company,b.company FROM demo.sales a LEFT JOIN demo.costs b ON a.company=b.company", "one") == [(101,None)]
        assert read("WITH s AS (SELECT company FROM demo.sales) SELECT * FROM s UNION ALL SELECT company FROM demo.costs", "one") == [(101,), (202,)]
        db.execute("DELETE FROM demo.access_map WHERE employee='one'")
        assert read("SELECT company FROM demo.sales", "one") == []
        assert read("SELECT company FROM demo.sales", "two") == [(202,)]


@pytest.mark.parametrize("sql, viewer", [
    ("SELECT * FROM demo.access_map", "one"), ("SELECT * FROM sales", "one"),
    ("SELECT * FROM another.demo.sales", "one"), ("SELECT * FROM demo.sales", None),
    ("SELECT * FROM demo.sales; SELECT * FROM demo.costs", "one"),
    ("SELECT * INTO demo.copy FROM demo.sales", "one"),
    ("SELECT unknown_function(company) FROM demo.sales", "one"),
])
def test_unclassified_input_is_denied(policy, sql, viewer):
    with pytest.raises(ValueError):
        protect_query(sql, policy, viewer)


def test_shared_report_rebuilds_only_a_live_projection():
    from types import SimpleNamespace
    from app.data_sources.fabric_company_rls import shared_query_code

    clients = {"finance": SimpleNamespace(company_filtered=True)}
    def saved(sql):
        return f"def generate_df(ds_clients, excel_files):\n    return ds_clients['finance'].execute_query({sql!r})"
    assert "execute_query" in shared_query_code(saved("SELECT amount FROM demo.sales"), clients)
    for code in (saved("SELECT 'an old value' FROM demo.sales"),
                 saved("SELECT company AS private_caption FROM demo.sales"),
                 "def generate_df(ds_clients, excel_files):\n    return 'old snapshot'"):
        with pytest.raises(ValueError):
            shared_query_code(code, clients)


def test_binding_order_and_literals_survive_cte_rendering(policy):
    sql = "WITH a AS (SELECT * FROM demo.costs) SELECT '@bow_employee' AS tag FROM demo.sales UNION ALL SELECT '@bow_source_0' FROM a"
    statement, bindings = protect_query(sql, policy, "reader")
    assert bindings == ("reader", "costs", "reader", "sales")
    assert "'@bow_employee'" in statement and "'@bow_source_0'" in statement


def test_grant_table_mappings_must_be_unambiguous(policy):
    duplicate_source = policy.model_dump(mode="json")
    duplicate_source["tables"][1]["source"] = duplicate_source["tables"][0]["source"]
    with pytest.raises(ValueError, match="own source value"):
        CompanyPolicy.model_validate(duplicate_source)

    duplicate_column = policy.model_dump(mode="json")
    duplicate_column["source_column"] = duplicate_column["employee_column"].upper()
    with pytest.raises(ValueError, match="distinct grant columns"):
        CompanyPolicy.model_validate(duplicate_column)


def test_protected_projection_scopes_star_predicates_and_new_columns(policy):
    data = policy.model_dump()
    data["tables"][0]["columns"] = {"company": None, "amount": ["finance@example.com"]}
    scoped = CompanyPolicy.model_validate(data)
    with sqlite3.connect(":memory:") as db:
        db.create_function("CONCAT", 2, lambda a, b: str(a if a is not None else "") + b)
        db.create_collation("Latin1_General_100_BIN2_UTF8", lambda a, b: (a > b) - (a < b))
        db.executescript("""
            ATTACH ':memory:' AS demo;
            CREATE TABLE demo.sales(company INTEGER, amount INTEGER, added_later TEXT);
            CREATE TABLE demo.access_map(employee TEXT, company INTEGER, source TEXT);
            INSERT INTO demo.sales VALUES (1,10,'private');
            INSERT INTO demo.access_map VALUES ('member@example.com',1,'sales'),
                                              ('finance@example.com',1,'sales');
        """)
        for viewer, expected in (("member@example.com", (1,)), ("finance@example.com", (1,10))):
            statement, params = protect_query("SELECT * FROM demo.sales", scoped, viewer)
            assert db.execute(statement, params).fetchone() == expected
        statement, params = protect_query("SELECT company FROM demo.sales WHERE amount > 0", scoped, "member@example.com")
        with pytest.raises(sqlite3.OperationalError):
            db.execute(statement, params)


@pytest.mark.parametrize("teams, expected", [(["east", "west"], [7, 9]), (["west"], [9]), ([], [])])
def test_project_grants_follow_reader_teams(teams, expected):
    from app.data_sources.fast.rls import Identity
    policy = CompanyPolicy.model_validate(dict(
        grants_table="demo.permissions", grant_principal="custom.teams",
        employee_column="team_id", company_column="project_id",
        tables=[{"table": "demo.work", "company_column": "project_id"}],
    ))
    identity = Identity(user_id="viewer", email="viewer@example.com", custom_attributes={"teams": teams})
    sql, bindings = protect_query("SELECT project_id FROM demo.work ORDER BY project_id", policy, identity.email, identity=identity)
    with sqlite3.connect(":memory:") as db:
        db.create_function("CONCAT", 2, lambda a,b: str(a)+str(b))
        db.create_collation("Latin1_General_100_BIN2_UTF8", lambda a,b: (a>b)-(a<b))
        db.executescript("""
            ATTACH ':memory:' AS demo;
            CREATE TABLE demo.work(project_id INTEGER);
            INSERT INTO demo.work VALUES (7), (9), (10);
            CREATE TABLE demo.permissions(team_id TEXT, project_id INTEGER);
            INSERT INTO demo.permissions VALUES ('east',7), ('west',9), ('viewer@example.com',10);
        """)
        assert [row[0] for row in db.execute(sql, bindings)] == expected


@pytest.mark.parametrize("dialect", ["fabric", "tsql", "postgres", "oracle"])
def test_grant_probe_and_read_bind_the_same_principals(policy, dialect):
    from app.data_sources.fast.rls import Identity
    fields = policy.model_dump()
    if dialect == "oracle":
        for key in ("grants_table", "employee_column", "company_column", "source_column"):
            fields[key] = fields[key].upper()
        for rule in fields["tables"]:
            rule["table"] = rule["table"].upper()
            rule["company_column"] = rule["company_column"].upper()
    configured = CompanyPolicy.model_validate({**fields, "grant_principal": "roles"})
    identity = Identity(user_id="viewer", role_names={"planning", "delivery"})
    for probe in (False, True):
        statement, bindings = protect_query("SELECT * FROM demo.sales", configured, "person@example.com",
                                           identity=identity, dialect=dialect, grant_probe=probe)
        values = list(bindings.values()) if isinstance(bindings, dict) else list(bindings)
        assert sorted(values) == ["delivery", "planning", "sales"]
        assert all(value not in statement for value in ("delivery", "planning", "person@example.com"))


def test_lookup_rejects_incomplete_resource_type_configuration(policy):
    data = policy.model_dump()
    data["source_column"] = None
    with pytest.raises(ValueError, match="configured together"):
        CompanyPolicy.model_validate(data)
    data = policy.model_dump()
    data["tables"][0]["source"] = None
    with pytest.raises(ValueError, match="configured together"):
        CompanyPolicy.model_validate(data)
