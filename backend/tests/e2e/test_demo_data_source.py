import pytest
from pathlib import Path


# Paths to demo database files
CHINOOK_DB_PATH = (Path(__file__).resolve().parent.parent.parent / "demo-datasources" / "chinook.sqlite").resolve()
STOCKS_DB_PATH = (Path(__file__).resolve().parent.parent.parent / "demo-datasources" / "stocks.duckdb").resolve()


@pytest.mark.e2e
def test_list_demo_data_sources(
    list_demo_data_sources,
    create_user,
    login_user,
    whoami,
):
    """Test listing available demo data sources."""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # List demo data sources
    demos = list_demo_data_sources(user_token=user_token, org_id=org_id)

    # Verify we get a list
    assert isinstance(demos, list)
    assert len(demos) >= 2  # At least chinook and stocks

    # Verify structure of demo items
    demo_ids = [d["id"] for d in demos]
    assert "chinook" in demo_ids
    assert "stocks" in demo_ids

    # Verify chinook demo structure
    chinook = next(d for d in demos if d["id"] == "chinook")
    assert chinook["name"] == "Music Store"
    assert chinook["type"] == "sqlite"
    assert "description" in chinook
    assert chinook["installed"] is False
    assert chinook["installed_data_source_id"] is None

    # Verify stocks demo structure
    stocks = next(d for d in demos if d["id"] == "stocks")
    assert stocks["name"] == "Financial Market Agent"
    assert stocks["type"] == "duckdb"
    assert stocks["installed"] is False


@pytest.mark.e2e
def test_install_chinook_demo(
    list_demo_data_sources,
    install_demo_data_source,
    get_data_sources,
    get_schema,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test installing the Chinook demo data source."""
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo database missing at {CHINOOK_DB_PATH}")

    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Install chinook demo
    result = install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )

    # Verify installation success
    assert result["success"] is True
    assert result["data_source_id"] is not None
    assert result["already_installed"] is False
    assert "Successfully installed" in result["message"]

    data_source_id = result["data_source_id"]

    # Verify data source appears in list
    data_sources = get_data_sources(user_token=user_token, org_id=org_id)
    assert any(ds["id"] == data_source_id for ds in data_sources)

    # Find the installed data source
    installed_ds = next(ds for ds in data_sources if ds["id"] == data_source_id)
    assert installed_ds["name"] == "Music Store"
    assert installed_ds["type"] == "sqlite"

    # Verify schema was loaded
    schema = get_schema(
        data_source_id=data_source_id,
        user_token=user_token,
        org_id=org_id,
    )
    assert isinstance(schema, list)
    assert len(schema) > 0
    table_names = [t["name"] for t in schema]
    assert "Album" in table_names or "albums" in [t.lower() for t in table_names]

    # Verify demo is now marked as installed
    demos = list_demo_data_sources(user_token=user_token, org_id=org_id)
    chinook = next(d for d in demos if d["id"] == "chinook")
    assert chinook["installed"] is True
    assert chinook["installed_data_source_id"] == data_source_id

    # Cleanup
    delete_data_source(
        data_source_id=data_source_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_install_demo_already_installed(
    install_demo_data_source,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that installing an already installed demo returns the existing data source."""
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo database missing at {CHINOOK_DB_PATH}")

    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Install chinook demo first time
    first_install = install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )
    assert first_install["success"] is True
    assert first_install["already_installed"] is False
    first_ds_id = first_install["data_source_id"]

    # Try to install again
    second_install = install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )

    # Should return success but indicate already installed
    assert second_install["success"] is True
    assert second_install["already_installed"] is True
    assert second_install["data_source_id"] == first_ds_id

    # Cleanup
    delete_data_source(
        data_source_id=first_ds_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_install_invalid_demo(
    install_demo_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that installing a non-existent demo returns an error."""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Try to install non-existent demo
    result = install_demo_data_source(
        demo_id="non_existent_demo",
        user_token=user_token,
        org_id=org_id,
    )

    # Should return failure
    assert result["success"] is False
    assert "not found" in result["message"].lower()


@pytest.mark.e2e
def test_install_stocks_demo(
    list_demo_data_sources,
    install_demo_data_source,
    get_data_sources,
    get_schema,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test installing the Financial Markets (stocks) demo data source."""
    if not STOCKS_DB_PATH.exists():
        pytest.skip(f"Stocks demo database missing at {STOCKS_DB_PATH}")

    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Install stocks demo
    result = install_demo_data_source(
        demo_id="stocks",
        user_token=user_token,
        org_id=org_id,
    )

    # Verify installation success
    assert result["success"] is True
    assert result["data_source_id"] is not None
    assert result["already_installed"] is False

    data_source_id = result["data_source_id"]

    # Verify data source appears in list
    data_sources = get_data_sources(user_token=user_token, org_id=org_id)
    installed_ds = next(ds for ds in data_sources if ds["id"] == data_source_id)
    assert installed_ds["name"] == "Financial Market Agent"
    assert installed_ds["type"] == "duckdb"

    # Verify schema was loaded
    schema = get_schema(
        data_source_id=data_source_id,
        user_token=user_token,
        org_id=org_id,
    )
    assert isinstance(schema, list)
    assert len(schema) > 0

    # Verify demo is now marked as installed
    demos = list_demo_data_sources(user_token=user_token, org_id=org_id)
    stocks = next(d for d in demos if d["id"] == "stocks")
    assert stocks["installed"] is True
    assert stocks["installed_data_source_id"] == data_source_id

    # Cleanup
    delete_data_source(
        data_source_id=data_source_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_demo_creates_instructions(
    install_demo_data_source,
    get_instructions,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that installing a demo creates the associated instructions."""
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo database missing at {CHINOOK_DB_PATH}")

    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Install chinook demo
    result = install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )
    assert result["success"] is True
    data_source_id = result["data_source_id"]

    # Get instructions for the data source
    instructions = get_instructions(
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source_id,
    )

    # Verify instructions were created
    assert isinstance(instructions, list)
    assert len(instructions) >= 2  # Chinook has at least 2 instructions

    # Check for expected instruction content
    instruction_texts = [i["text"].lower() for i in instructions]
    assert any("vip" in text for text in instruction_texts)
    assert any("bar chart" in text or "top" in text for text in instruction_texts)

    # Cleanup
    delete_data_source(
        data_source_id=data_source_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_demo_creates_connection(
    install_demo_data_source,
    get_connections,
    get_data_sources,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that installing a demo creates both domain and connection."""
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo database missing at {CHINOOK_DB_PATH}")

    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Install chinook demo
    result = install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )
    assert result["success"] is True
    data_source_id = result["data_source_id"]

    # Verify connection was created
    connections = get_connections(user_token=user_token, org_id=org_id)
    assert isinstance(connections, list)
    assert len(connections) >= 1

    # Find connection associated with this demo (should have matching name or type)
    chinook_connection = None
    for conn in connections:
        if conn["type"] == "sqlite" and "Chinook" in conn["name"]:
            chinook_connection = conn
            break

    assert chinook_connection is not None, "Connection for Chinook demo should exist"
    assert chinook_connection["domain_count"] >= 1

    # Verify domain has connection info in response
    domains = get_data_sources(user_token=user_token, org_id=org_id)
    our_domain = next((d for d in domains if d["id"] == data_source_id), None)
    assert our_domain is not None

    # Cleanup
    delete_data_source(
        data_source_id=data_source_id,
        user_token=user_token,
        org_id=org_id,
    )
