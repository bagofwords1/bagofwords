import pytest


# ============================================================
# Suite Tests
# ============================================================

@pytest.mark.e2e
def test_create_and_get_suite(create_user, login_user, whoami, get_test_suites, create_test_suite, get_test_suite):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    initial = get_test_suites(user_token=token, org_id=org_id)
    new_suite = create_test_suite(name="E2E Suite", description="End-to-end suite", user_token=token, org_id=org_id)
    assert new_suite["name"] == "E2E Suite"

    after = get_test_suites(user_token=token, org_id=org_id)
    assert isinstance(after, list)
    assert len(after) == len(initial) + 1

    resp = get_test_suite(new_suite["id"], user_token=token, org_id=org_id)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == new_suite["id"]
    assert data["name"] == "E2E Suite"


@pytest.mark.e2e
def test_get_suite_404(create_user, login_user, whoami, get_test_suite):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = get_test_suite("non-existent-suite-id", user_token=token, org_id=org_id)
    assert resp.status_code == 404
    assert "Test suite not found" in resp.json()["detail"]


# ============================================================
# Case Tests
# ============================================================

@pytest.mark.e2e
def test_create_case_and_get(create_user, login_user, whoami, create_test_suite, get_test_cases, create_test_case, get_test_case):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Cases Suite", user_token=token, org_id=org_id)
    cases_before = get_test_cases(suite["id"], user_token=token, org_id=org_id)
    assert isinstance(cases_before, list)

    case = create_test_case(
        suite_id=suite["id"],
        name="Case 1",
        user_token=token,
        org_id=org_id,
    )
    assert case["name"] == "Case 1"
    assert "prompt_json" in case
    assert case["prompt_json"].get("content") == "Evaluate this output."

    cases_after = get_test_cases(suite["id"], user_token=token, org_id=org_id)
    assert len(cases_after) == len(cases_before) + 1

    resp = get_test_case(case["id"], user_token=token, org_id=org_id)
    assert resp.status_code == 200
    got = resp.json()
    assert got["id"] == case["id"]
    assert got["name"] == "Case 1"
    assert got["suite_id"] == suite["id"]
    assert got["prompt_json"]["content"] == "Evaluate this output."


@pytest.mark.e2e
def test_get_case_404(create_user, login_user, whoami, get_test_case):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = get_test_case("non-existent-case-id", user_token=token, org_id=org_id)
    assert resp.status_code == 404
    assert "Test case not found" in resp.json()["detail"]


# ============================================================
# Test Run Tests with Build System Integration
# ============================================================
# Note: These tests verify the build_id parameter is accepted by the API.
# They don't actually execute agent runs (which require LLM config).

@pytest.mark.e2e
def test_test_run_schema_includes_build_fields(
    create_user, login_user, whoami,
    create_test_suite, create_test_case,
    create_instruction, get_main_build,
    test_client
):
    """Verify TestRun schema accepts build_id and returns build info."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # Create an instruction to trigger a build
    create_instruction(
        text="Test content for build",
        status="published",
        user_token=token,
        org_id=org_id,
    )

    # Get the main build
    main_build = get_main_build(user_token=token, org_id=org_id)
    assert main_build is not None, "Main build should exist after creating instruction"

    # Create a test suite and case
    suite = create_test_suite(name="Build Test Suite", user_token=token, org_id=org_id)
    case = create_test_case(
        suite_id=suite["id"],
        name="Build Test Case",
        user_token=token,
        org_id=org_id,
    )

    # Verify API accepts build_id in request (even if run fails due to no LLM)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    payload = {
        "case_ids": [case["id"]],
        "trigger_reason": "manual",
        "build_id": main_build["id"],  # This should be accepted
    }
    
    # The request should be accepted (200) or fail with LLM config error (not 422 validation error)
    response = test_client.post("/api/tests/runs", json=payload, headers=headers)
    
    # If it succeeds, verify build info is in response
    if response.status_code == 200:
        data = response.json()
        assert "build_id" in data, "Response should include build_id field"
        assert "build_number" in data, "Response should include build_number field"
    else:
        # If it fails, it should be LLM config error, not validation error
        assert response.status_code != 422, f"API should accept build_id parameter, got: {response.json()}"


@pytest.mark.e2e
def test_builds_api_returns_list(
    create_user, login_user, whoami,
    create_instruction, update_instruction,
    get_builds
):
    """Verify builds API returns proper list for test run integration."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # Create and update instruction to generate builds
    inst = create_instruction(
        text="Initial content",
        status="published",
        user_token=token,
        org_id=org_id,
    )

    # Update to create another build
    update_instruction(
        inst["id"],
        text="Updated content",
        user_token=token,
        org_id=org_id,
    )

    # Get all builds
    builds_response = get_builds(user_token=token, org_id=org_id)
    
    # Handle both list and dict with items
    if isinstance(builds_response, dict) and "items" in builds_response:
        builds = builds_response["items"]
    else:
        builds = builds_response
    
    assert isinstance(builds, list), f"Builds should be a list, got {type(builds)}"
    assert len(builds) >= 2, "Should have at least 2 builds after update"

    # Verify each build has required fields for test run integration
    for build in builds:
        assert "id" in build, "Build should have id"
        assert "build_number" in build, "Build should have build_number"
        assert "is_main" in build, "Build should have is_main flag"


@pytest.mark.e2e
def test_main_build_available_for_test_runs(
    create_user, login_user, whoami,
    create_instruction, get_main_build
):
    """Verify main build is available for test run integration."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # Create instruction to have a build
    create_instruction(
        text="Test content for build",
        status="published",
        user_token=token,
        org_id=org_id,
    )

    # Get main build
    main_build = get_main_build(user_token=token, org_id=org_id)
    
    assert main_build is not None, "Main build should exist"
    assert "id" in main_build, "Main build should have id"
    assert "build_number" in main_build, "Main build should have build_number"
    assert main_build.get("is_main") == True, "Main build should have is_main=True"


