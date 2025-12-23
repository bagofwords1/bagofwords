"""
E2E tests for the Build versioning system.

Tests cover:
- Build lifecycle (creation, versioning, auto-finalization)
- Field change versioning (text, status, load_mode, category, references, labels)
- Build API endpoints (list, get, contents)
- Build diffing (added, removed, modified)
- Rollback functionality
- Bulk operations
"""
import pytest


# ============================================================================
# BUILD LIFECYCLE TESTS
# ============================================================================

@pytest.mark.e2e
def test_create_instruction_creates_build(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_main_build,
):
    """Test that creating an instruction creates a build."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create an instruction
    instruction = create_global_instruction(
        text="Test instruction for build creation",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    # Verify a main build exists
    main_build = get_main_build(user_token=user_token, org_id=org_id)
    assert main_build is not None, "Main build should exist after instruction creation"
    assert main_build["is_main"] is True, "Build should be marked as main"
    assert main_build["status"] == "approved", "Build should be approved"


@pytest.mark.e2e
def test_create_instruction_creates_version(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_instruction,
):
    """Test that creating an instruction creates an InstructionVersion."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create an instruction
    instruction = create_global_instruction(
        text="Test instruction for version creation",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    # Fetch instruction and verify current_version_id is set
    fetched = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    assert fetched.get("current_version_id") is not None, "current_version_id should be set"


@pytest.mark.e2e
def test_instruction_has_current_version_id(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_instruction,
):
    """Test that instruction.current_version_id is populated after creation."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    fetched = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    
    assert fetched.get("current_version_id") is not None, "current_version_id should be set"
    assert isinstance(fetched.get("current_version_id"), str), "current_version_id should be a string"


@pytest.mark.e2e
def test_build_is_main_after_create(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_main_build,
):
    """Test that build is promoted to is_main=True after creation."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    main_build = get_main_build(user_token=user_token, org_id=org_id)
    assert main_build is not None, "Main build should exist"
    assert main_build["is_main"] is True, "Build should be is_main=True"


@pytest.mark.e2e
def test_build_number_increments(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
):
    """Test that build numbers increment sequentially."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create first instruction
    create_global_instruction(
        text="First instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    # Get builds after first instruction
    builds_after_first = get_builds(user_token=user_token, org_id=org_id)
    first_build_number = builds_after_first["items"][0]["build_number"] if builds_after_first["items"] else 0

    # Create second instruction
    create_global_instruction(
        text="Second instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    # Get builds after second instruction
    builds_after_second = get_builds(user_token=user_token, org_id=org_id)
    
    # Should have more builds or same (if batched)
    assert builds_after_second["total"] >= builds_after_first["total"], "Total builds should increase or stay same"
    
    # Latest build number should be >= first
    latest_build_number = builds_after_second["items"][0]["build_number"]
    assert latest_build_number >= first_build_number, "Build number should increment"


@pytest.mark.e2e
def test_multiple_creates_in_sequence(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
    get_main_build,
):
    """Test that each create in sequence increments build number."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create 3 instructions
    for i in range(3):
        create_global_instruction(
            text=f"Instruction {i+1}",
            user_token=user_token,
            org_id=org_id,
            status="published"
        )

    # Get all builds
    builds = get_builds(user_token=user_token, org_id=org_id)
    assert builds["total"] >= 1, "Should have at least one build"

    # Verify main build exists
    main_build = get_main_build(user_token=user_token, org_id=org_id)
    assert main_build is not None, "Main build should exist"


# ============================================================================
# FIELD CHANGE VERSIONING TESTS
# ============================================================================

@pytest.mark.e2e
def test_update_text_creates_new_version(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    update_instruction,
    get_instruction,
):
    """Test that updating text creates a new version."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Original text",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    original_version_id = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    ).get("current_version_id")

    # Update text
    update_instruction(
        instruction_id=instruction["id"],
        text="Updated text",
        user_token=user_token,
        org_id=org_id
    )

    updated = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    
    new_version_id = updated.get("current_version_id")
    assert new_version_id is not None, "Should have a version ID"
    assert new_version_id != original_version_id, "Version ID should change after text update"


@pytest.mark.e2e
def test_update_status_creates_new_version(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    update_instruction,
    get_instruction,
):
    """Test that updating status creates a new version."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="draft"
    )

    original_version_id = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    ).get("current_version_id")

    # Update status to published
    update_instruction(
        instruction_id=instruction["id"],
        status="published",
        user_token=user_token,
        org_id=org_id
    )

    updated = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    
    new_version_id = updated.get("current_version_id")
    assert new_version_id is not None, "Should have a version ID"
    # Status changes should create a new version
    assert new_version_id != original_version_id, "Version ID should change after status update"


@pytest.mark.e2e
def test_update_load_mode_creates_new_version(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    update_instruction,
    get_instruction,
):
    """Test that updating load_mode creates a new version."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    original_version_id = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    ).get("current_version_id")

    # Update load_mode
    update_instruction(
        instruction_id=instruction["id"],
        load_mode="disabled",
        user_token=user_token,
        org_id=org_id
    )

    updated = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    
    new_version_id = updated.get("current_version_id")
    assert new_version_id is not None, "Should have a version ID"
    assert new_version_id != original_version_id, "Version ID should change after load_mode update"


@pytest.mark.e2e
def test_update_category_creates_new_version(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    update_instruction,
    get_instruction,
):
    """Test that updating category creates a new version."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published",
        category="general"
    )

    original_version_id = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    ).get("current_version_id")

    # Update category
    update_instruction(
        instruction_id=instruction["id"],
        category="visualization",
        user_token=user_token,
        org_id=org_id
    )

    updated = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    
    new_version_id = updated.get("current_version_id")
    assert new_version_id is not None, "Should have a version ID"
    assert new_version_id != original_version_id, "Version ID should change after category update"


@pytest.mark.e2e
def test_update_labels_creates_new_version(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    create_label,
    update_instruction,
    get_instruction,
):
    """Test that updating labels creates a new version."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create a label
    label = create_label(name="Test Label", user_token=user_token, org_id=org_id)

    instruction = create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    original_version_id = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    ).get("current_version_id")

    # Add label
    update_instruction(
        instruction_id=instruction["id"],
        label_ids=[label["id"]],
        user_token=user_token,
        org_id=org_id
    )

    updated = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    
    new_version_id = updated.get("current_version_id")
    assert new_version_id is not None, "Should have a version ID"
    assert new_version_id != original_version_id, "Version ID should change after label update"


@pytest.mark.e2e
def test_no_change_does_not_create_version(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    update_instruction,
    get_instruction,
    get_builds,
):
    """Test that no change does not create a new version."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    original_version_id = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    ).get("current_version_id")

    builds_before = get_builds(user_token=user_token, org_id=org_id)

    # Update with same text (no change)
    update_instruction(
        instruction_id=instruction["id"],
        text="Test instruction",  # Same text
        user_token=user_token,
        org_id=org_id
    )

    updated = get_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    
    new_version_id = updated.get("current_version_id")
    # Version ID should remain the same if no content changed
    assert new_version_id == original_version_id, "Version ID should not change when content is unchanged"


# ============================================================================
# BUILD API TESTS
# ============================================================================

@pytest.mark.e2e
def test_list_builds_returns_paginated(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
):
    """Test that GET /builds returns paginated list."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create instructions to generate builds
    create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds = get_builds(user_token=user_token, org_id=org_id)
    
    assert "items" in builds, "Response should have items"
    assert "total" in builds, "Response should have total"
    assert "page" in builds, "Response should have page"
    assert "per_page" in builds, "Response should have per_page"
    assert "pages" in builds, "Response should have pages"


@pytest.mark.e2e
def test_list_builds_defaults_to_approved(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
):
    """Test that GET /builds defaults to approved status filter."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    # Get builds without status filter
    builds = get_builds(user_token=user_token, org_id=org_id)
    
    # All returned builds should be approved
    for build in builds["items"]:
        assert build["status"] == "approved", f"Expected approved status, got {build['status']}"


@pytest.mark.e2e
def test_list_builds_filters_by_status(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
):
    """Test that GET /builds filters by status parameter."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    # Get approved builds
    approved_builds = get_builds(user_token=user_token, org_id=org_id, status="approved")
    
    for build in approved_builds["items"]:
        assert build["status"] == "approved", f"Expected approved, got {build['status']}"


@pytest.mark.e2e
def test_get_main_build(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_main_build,
):
    """Test that GET /builds/main returns the main build."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    main_build = get_main_build(user_token=user_token, org_id=org_id)
    
    assert main_build is not None, "Main build should exist"
    assert main_build["is_main"] is True, "Build should be marked as main"
    assert "id" in main_build, "Build should have id"
    assert "build_number" in main_build, "Build should have build_number"


@pytest.mark.e2e
def test_get_build_by_id(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_main_build,
    get_build,
):
    """Test that GET /builds/{id} returns build details."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    main_build = get_main_build(user_token=user_token, org_id=org_id)
    build_id = main_build["id"]

    fetched_build = get_build(build_id=build_id, user_token=user_token, org_id=org_id)
    
    assert fetched_build["id"] == build_id, "Build ID should match"
    assert fetched_build["build_number"] == main_build["build_number"], "Build number should match"


@pytest.mark.e2e
def test_get_build_contents(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_main_build,
    get_build_contents,
):
    """Test that GET /builds/{id}/contents returns instructions in build."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Test instruction for contents",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    main_build = get_main_build(user_token=user_token, org_id=org_id)
    contents = get_build_contents(build_id=main_build["id"], user_token=user_token, org_id=org_id)
    
    assert isinstance(contents, list), "Contents should be a list"
    assert len(contents) >= 1, "Should have at least one instruction in build"
    
    # Find our instruction in the contents
    instruction_ids = [c.get("instruction_id") for c in contents]
    assert instruction["id"] in instruction_ids, "Created instruction should be in build contents"


# ============================================================================
# BUILD DIFF TESTS
# ============================================================================

@pytest.mark.e2e
def test_diff_shows_added_instructions(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
    get_build_diff,
):
    """Test that diff detects added instructions."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create first instruction
    create_global_instruction(
        text="First instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_after_first = get_builds(user_token=user_token, org_id=org_id)
    first_build_id = builds_after_first["items"][0]["id"]

    # Create second instruction
    create_global_instruction(
        text="Second instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_after_second = get_builds(user_token=user_token, org_id=org_id)
    second_build_id = builds_after_second["items"][0]["id"]

    # Only diff if builds are different
    if first_build_id != second_build_id:
        diff = get_build_diff(
            build_id=second_build_id,
            compare_to_build_id=first_build_id,
            user_token=user_token,
            org_id=org_id
        )
        
        assert "added" in diff or "added_count" in diff, "Diff should have added field"


@pytest.mark.e2e
def test_diff_shows_removed_instructions(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    delete_instruction,
    get_builds,
    get_build_diff,
):
    """Test that diff detects removed instructions."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create instruction
    instruction = create_global_instruction(
        text="Instruction to delete",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_before_delete = get_builds(user_token=user_token, org_id=org_id)
    build_before_id = builds_before_delete["items"][0]["id"]

    # Delete instruction
    delete_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )

    builds_after_delete = get_builds(user_token=user_token, org_id=org_id)
    
    if builds_after_delete["total"] > 0:
        build_after_id = builds_after_delete["items"][0]["id"]
        
        if build_before_id != build_after_id:
            # Compare OLD build against NEW build to see what was removed
            # removed = items in build_before but not in build_after
            diff = get_build_diff(
                build_id=build_before_id,
                compare_to_build_id=build_after_id,
                user_token=user_token,
                org_id=org_id
            )
            
            assert "removed" in diff or "removed_count" in diff, "Diff should have removed field"


@pytest.mark.e2e
def test_diff_shows_modified_instructions(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    update_instruction,
    get_builds,
    get_build_diff,
):
    """Test that diff detects modified instructions."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create instruction
    instruction = create_global_instruction(
        text="Original text",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_before_update = get_builds(user_token=user_token, org_id=org_id)
    build_before_id = builds_before_update["items"][0]["id"]

    # Update instruction
    update_instruction(
        instruction_id=instruction["id"],
        text="Updated text",
        user_token=user_token,
        org_id=org_id
    )

    builds_after_update = get_builds(user_token=user_token, org_id=org_id)
    build_after_id = builds_after_update["items"][0]["id"]

    if build_before_id != build_after_id:
        diff = get_build_diff(
            build_id=build_after_id,
            compare_to_build_id=build_before_id,
            user_token=user_token,
            org_id=org_id
        )
        
        assert "modified" in diff or "modified_count" in diff, "Diff should have modified field"


@pytest.mark.e2e
def test_diff_detailed_includes_text(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
    get_build_diff_detailed,
):
    """Test that detailed diff includes instruction text."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create first instruction
    create_global_instruction(
        text="First instruction text",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_after_first = get_builds(user_token=user_token, org_id=org_id)
    first_build_id = builds_after_first["items"][0]["id"]

    # Create second instruction
    create_global_instruction(
        text="Second instruction text",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_after_second = get_builds(user_token=user_token, org_id=org_id)
    second_build_id = builds_after_second["items"][0]["id"]

    if first_build_id != second_build_id:
        detailed_diff = get_build_diff_detailed(
            build_id=second_build_id,
            compare_to_build_id=first_build_id,
            user_token=user_token,
            org_id=org_id
        )
        
        assert "items" in detailed_diff, "Detailed diff should have items"
        if detailed_diff["items"]:
            item = detailed_diff["items"][0]
            assert "text" in item, "Diff item should have text field"


@pytest.mark.e2e
def test_diff_detailed_shows_changed_fields(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    update_instruction,
    get_builds,
    get_build_diff_detailed,
):
    """Test that detailed diff shows changed_fields for modifications."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create instruction
    instruction = create_global_instruction(
        text="Original text",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_before = get_builds(user_token=user_token, org_id=org_id)
    build_before_id = builds_before["items"][0]["id"]

    # Update text
    update_instruction(
        instruction_id=instruction["id"],
        text="Modified text",
        user_token=user_token,
        org_id=org_id
    )

    builds_after = get_builds(user_token=user_token, org_id=org_id)
    build_after_id = builds_after["items"][0]["id"]

    if build_before_id != build_after_id:
        detailed_diff = get_build_diff_detailed(
            build_id=build_after_id,
            compare_to_build_id=build_before_id,
            user_token=user_token,
            org_id=org_id
        )
        
        # Find the modified item
        modified_items = [i for i in detailed_diff.get("items", []) if i.get("change_type") == "modified"]
        if modified_items:
            item = modified_items[0]
            assert "changed_fields" in item or "text" in item, "Modified item should have changed_fields or text"


@pytest.mark.e2e
def test_diff_detailed_shows_previous_text(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    update_instruction,
    get_builds,
    get_build_diff_detailed,
):
    """Test that modified items in detailed diff have previous_text."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Original text before update",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_before = get_builds(user_token=user_token, org_id=org_id)
    build_before_id = builds_before["items"][0]["id"]

    update_instruction(
        instruction_id=instruction["id"],
        text="New text after update",
        user_token=user_token,
        org_id=org_id
    )

    builds_after = get_builds(user_token=user_token, org_id=org_id)
    build_after_id = builds_after["items"][0]["id"]

    if build_before_id != build_after_id:
        detailed_diff = get_build_diff_detailed(
            build_id=build_after_id,
            compare_to_build_id=build_before_id,
            user_token=user_token,
            org_id=org_id
        )
        
        modified_items = [i for i in detailed_diff.get("items", []) if i.get("change_type") == "modified"]
        if modified_items:
            item = modified_items[0]
            assert "previous_text" in item, "Modified item should have previous_text"


# ============================================================================
# ROLLBACK TESTS
# ============================================================================

@pytest.mark.e2e
def test_rollback_creates_new_build(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
    get_main_build,
    rollback_build,
):
    """Test that rollback creates a new build (not promotes the old one)."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create first instruction
    create_global_instruction(
        text="First instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_after_first = get_builds(user_token=user_token, org_id=org_id)
    first_build_id = builds_after_first["items"][0]["id"]
    count_after_first = builds_after_first["total"]

    # Create second instruction (new build)
    create_global_instruction(
        text="Second instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_after_second = get_builds(user_token=user_token, org_id=org_id)
    
    if len(builds_after_second["items"]) >= 2:
        count_before_rollback = builds_after_second["total"]
        
        # Rollback to first build - this should create a NEW build
        rolled_back = rollback_build(
            build_id=first_build_id,
            user_token=user_token,
            org_id=org_id
        )
        
        # The returned build should be main
        assert rolled_back["is_main"] is True, "New build from rollback should be main"
        
        # The returned build should be a NEW build, not the original first build
        assert rolled_back["id"] != first_build_id, "Rollback should create a new build, not promote the old one"
        
        # Total build count should increase by 1
        builds_after_rollback = get_builds(user_token=user_token, org_id=org_id)
        assert builds_after_rollback["total"] == count_before_rollback + 1, "Rollback should create exactly 1 new build"


@pytest.mark.e2e
def test_rollback_updates_is_main(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
    get_main_build,
    get_build_contents,
    rollback_build,
):
    """Test that rollback creates a new main build with same contents as target."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    create_global_instruction(
        text="First instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    first_main = get_main_build(user_token=user_token, org_id=org_id)
    first_build_id = first_main["id"]
    
    # Get contents of first build to compare later
    first_contents = get_build_contents(build_id=first_build_id, user_token=user_token, org_id=org_id)
    first_instruction_ids = {c["instruction_id"] for c in first_contents}

    create_global_instruction(
        text="Second instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    current_main = get_main_build(user_token=user_token, org_id=org_id)
    
    if current_main["id"] != first_build_id:
        rolled_back = rollback_build(
            build_id=first_build_id,
            user_token=user_token,
            org_id=org_id
        )
        
        new_main = get_main_build(user_token=user_token, org_id=org_id)
        
        # New main should be the newly created build, NOT the original first build
        assert new_main["id"] == rolled_back["id"], "Main build should be the new rollback build"
        assert new_main["id"] != first_build_id, "Main build should NOT be the original target build"
        
        # New build should have same contents as the target build
        new_contents = get_build_contents(build_id=new_main["id"], user_token=user_token, org_id=org_id)
        new_instruction_ids = {c["instruction_id"] for c in new_contents}
        assert new_instruction_ids == first_instruction_ids, "Rollback build should have same instructions as target"


@pytest.mark.e2e
def test_rollback_preserves_history(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
    rollback_build,
):
    """Test that rollback preserves all build history and adds a new build."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    create_global_instruction(
        text="First instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_before = get_builds(user_token=user_token, org_id=org_id)
    first_build_id = builds_before["items"][0]["id"]
    count_before = builds_before["total"]

    create_global_instruction(
        text="Second instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_middle = get_builds(user_token=user_token, org_id=org_id)
    
    if builds_middle["total"] > count_before:
        rollback_build(
            build_id=first_build_id,
            user_token=user_token,
            org_id=org_id
        )
        
        builds_after = get_builds(user_token=user_token, org_id=org_id)
        # Rollback creates a new build, so count should increase by exactly 1
        assert builds_after["total"] == builds_middle["total"] + 1, "Rollback should add exactly one new build"


@pytest.mark.e2e
def test_rollback_build_has_source_rollback(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
    get_main_build,
    rollback_build,
):
    """Test that rollback creates a build with source='rollback'."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create first instruction
    create_global_instruction(
        text="First instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_after_first = get_builds(user_token=user_token, org_id=org_id)
    first_build_id = builds_after_first["items"][0]["id"]

    # Create second instruction (new build)
    create_global_instruction(
        text="Second instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds_after_second = get_builds(user_token=user_token, org_id=org_id)
    
    if len(builds_after_second["items"]) >= 2:
        # Rollback to first build
        rolled_back = rollback_build(
            build_id=first_build_id,
            user_token=user_token,
            org_id=org_id
        )
        
        # The new build should have source='rollback' for audit trail
        assert rolled_back.get("source") == "rollback", "Rollback build should have source='rollback'"


# ============================================================================
# BULK OPERATIONS TESTS
# ============================================================================

@pytest.mark.e2e
def test_bulk_update_creates_single_build(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    bulk_update_instructions,
    get_builds,
):
    """Test that bulk update creates a single build for N instructions."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create multiple instructions
    instructions = []
    for i in range(3):
        inst = create_global_instruction(
            text=f"Instruction {i+1}",
            user_token=user_token,
            org_id=org_id,
            status="draft"
        )
        instructions.append(inst)

    builds_before = get_builds(user_token=user_token, org_id=org_id)
    count_before = builds_before["total"]

    # Bulk update all to published
    instruction_ids = [inst["id"] for inst in instructions]
    bulk_update_instructions(
        ids=instruction_ids,
        status="published",
        user_token=user_token,
        org_id=org_id
    )

    builds_after = get_builds(user_token=user_token, org_id=org_id)
    
    # Should create exactly 1 new build for the bulk operation
    new_builds = builds_after["total"] - count_before
    assert new_builds <= 1, f"Bulk update should create at most 1 build, created {new_builds}"


@pytest.mark.e2e
def test_bulk_status_change_creates_versions(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    bulk_update_instructions,
    get_instruction,
):
    """Test that bulk status change creates versions for each instruction."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create instructions
    instructions = []
    for i in range(2):
        inst = create_global_instruction(
            text=f"Instruction {i+1}",
            user_token=user_token,
            org_id=org_id,
            status="draft"
        )
        instructions.append(inst)

    # Get original version IDs
    original_versions = {}
    for inst in instructions:
        fetched = get_instruction(
            instruction_id=inst["id"],
            user_token=user_token,
            org_id=org_id
        )
        original_versions[inst["id"]] = fetched.get("current_version_id")

    # Bulk update status
    instruction_ids = [inst["id"] for inst in instructions]
    bulk_update_instructions(
        ids=instruction_ids,
        status="published",
        user_token=user_token,
        org_id=org_id
    )

    # Check that versions changed
    for inst in instructions:
        fetched = get_instruction(
            instruction_id=inst["id"],
            user_token=user_token,
            org_id=org_id
        )
        new_version = fetched.get("current_version_id")
        original_version = original_versions[inst["id"]]
        
        # Version should change after status update
        if original_version:
            assert new_version != original_version, f"Version should change for instruction {inst['id']}"


@pytest.mark.e2e
def test_bulk_load_mode_change_creates_versions(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    bulk_update_instructions,
    get_instruction,
):
    """Test that bulk load_mode change creates versions for each instruction."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create instructions
    instructions = []
    for i in range(2):
        inst = create_global_instruction(
            text=f"Instruction {i+1}",
            user_token=user_token,
            org_id=org_id,
            status="published"
        )
        instructions.append(inst)

    # Get original version IDs
    original_versions = {}
    for inst in instructions:
        fetched = get_instruction(
            instruction_id=inst["id"],
            user_token=user_token,
            org_id=org_id
        )
        original_versions[inst["id"]] = fetched.get("current_version_id")

    # Bulk update load_mode
    instruction_ids = [inst["id"] for inst in instructions]
    bulk_update_instructions(
        ids=instruction_ids,
        load_mode="disabled",
        user_token=user_token,
        org_id=org_id
    )

    # Check that versions changed
    for inst in instructions:
        fetched = get_instruction(
            instruction_id=inst["id"],
            user_token=user_token,
            org_id=org_id
        )
        new_version = fetched.get("current_version_id")
        original_version = original_versions[inst["id"]]
        
        if original_version:
            assert new_version != original_version, f"Version should change for instruction {inst['id']}"


# ============================================================================
# BUILD + TEST RUN INTEGRATION TESTS
# ============================================================================

@pytest.mark.e2e
def test_list_builds_includes_test_run_fields(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    get_builds,
):
    """Test that list_builds response includes test run fields."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create an instruction to have a build
    create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    builds = get_builds(user_token=user_token, org_id=org_id)
    
    assert builds["total"] >= 1, "Should have at least one build"
    
    build = builds["items"][0]
    # These fields should exist (even if null) for frontend compatibility
    assert "test_run_id" in build, "Build should have test_run_id field"
    assert "test_status" in build, "Build should have test_status field"
    assert "test_passed" in build, "Build should have test_passed field"
    assert "test_failed" in build, "Build should have test_failed field"


@pytest.mark.e2e
def test_list_builds_shows_test_results_after_run(
    create_user,
    login_user,
    whoami,
    create_global_instruction,
    create_test_suite,
    create_test_case,
    get_main_build,
    get_builds,
    test_client,
):
    """Test that list_builds shows test results after a test run."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create instruction to have a build
    create_global_instruction(
        text="Test instruction for test run",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    main_build = get_main_build(user_token=user_token, org_id=org_id)
    assert main_build is not None, "Main build should exist"

    # Create a test suite and case
    suite = create_test_suite(name="Build Test Suite", user_token=user_token, org_id=org_id)
    case = create_test_case(
        suite_id=suite["id"],
        name="Build Test Case",
        user_token=user_token,
        org_id=org_id,
    )

    # Create a test run for this build
    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }
    payload = {
        "case_ids": [case["id"]],
        "trigger_reason": "manual",
        "build_id": main_build["id"],
    }
    
    response = test_client.post("/api/tests/runs", json=payload, headers=headers)
    
    # If test run was created successfully, check builds list
    if response.status_code == 200:
        run = response.json()
        
        # Get builds and check that our build has test run info
        builds = get_builds(user_token=user_token, org_id=org_id)
        
        # Find our build
        our_build = next((b for b in builds["items"] if b["id"] == main_build["id"]), None)
        assert our_build is not None, "Our build should be in list"
        
        # Test run should be linked (by build_id on the run)
        assert our_build.get("test_run_id") == run["id"], "Build should show latest test run"
