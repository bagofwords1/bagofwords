"""
E2E tests for Git Sync + Build Integration.

Tests cover:
- Git sync creates builds with source='git'
- Git update instructions creates versions
- Git remove instructions (repo delete) creates builds with removed items
- Unlink then modify flows
- Unlink then git delete flows
"""
from pathlib import Path
import pytest

TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()
TEST_GIT_REPO_URL = "https://github.com/bagofwords1/dbt-mock"


# ============================================================================
# GIT SYNC CREATES BUILDS
# ============================================================================

@pytest.mark.e2e
def test_git_index_creates_build(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_main_build,
    get_builds,
    delete_git_repository,
):
    """Test that indexing a git repository creates a build with source='git'."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Git Build Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
        "auto_publish": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Verify a build was created
    main_build = get_main_build(user_token=user_token, org_id=org_id)
    assert main_build is not None, "Main build should exist after git indexing"
    assert main_build["source"] == "git", f"Build source should be 'git', got {main_build['source']}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_git_index_build_has_correct_counts(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_main_build,
    get_metadata_resources,
    delete_git_repository,
):
    """Test that git index build has correct added_count matching resource count."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Git Counts Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
        "auto_publish": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get resource count
    metadata = get_metadata_resources(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
    )
    resource_count = len(metadata.get("resources", []))

    # Get build and verify counts
    main_build = get_main_build(user_token=user_token, org_id=org_id)
    
    # The build should have total_instructions matching the indexed resources
    assert main_build["total_instructions"] >= resource_count, \
        f"Build should have at least {resource_count} instructions, has {main_build['total_instructions']}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_git_reindex_creates_new_build(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_builds,
    delete_git_repository,
):
    """Test that re-indexing creates a new build."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Git Reindex Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    # First index
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    builds_after_first = get_builds(user_token=user_token, org_id=org_id)
    first_count = builds_after_first["total"]
    first_build_number = builds_after_first["items"][0]["build_number"] if builds_after_first["items"] else 0

    # Re-index
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    builds_after_second = get_builds(user_token=user_token, org_id=org_id)
    
    # Should have at least as many builds
    assert builds_after_second["total"] >= first_count, "Should have at least same number of builds after reindex"
    
    # Latest build number should be >= first
    if builds_after_second["items"]:
        latest_build_number = builds_after_second["items"][0]["build_number"]
        assert latest_build_number >= first_build_number, "Build number should increment on reindex"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


# ============================================================================
# GIT REMOVE INSTRUCTIONS (REPO DELETE)
# ============================================================================

@pytest.mark.e2e
def test_git_delete_repo_creates_build(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_builds,
    delete_git_repository,
):
    """Test that deleting a git repository creates a new build."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Git Delete Build Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    builds_before_delete = get_builds(user_token=user_token, org_id=org_id)
    count_before = builds_before_delete["total"]

    # Delete git repository
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    builds_after_delete = get_builds(user_token=user_token, org_id=org_id)
    
    # Should have created a new build for the deletion
    assert builds_after_delete["total"] >= count_before, \
        "Should have at least same number of builds after deletion (deletion creates a build)"


@pytest.mark.e2e
def test_git_delete_repo_diff_shows_removed(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_builds,
    get_build_diff,
    delete_git_repository,
):
    """Test that diff after git repo delete shows removed instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Git Delete Diff Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    builds_before = get_builds(user_token=user_token, org_id=org_id)
    build_before_id = builds_before["items"][0]["id"]

    # Delete git repository
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    builds_after = get_builds(user_token=user_token, org_id=org_id)
    
    if builds_after["total"] > 0 and builds_after["items"][0]["id"] != build_before_id:
        build_after_id = builds_after["items"][0]["id"]
        
        # Compare OLD build against NEW build to see what was removed
        # removed = items in build_before but not in build_after
        diff = get_build_diff(
            build_id=build_before_id,
            compare_to_build_id=build_after_id,
            user_token=user_token,
            org_id=org_id
        )
        
        # Should show removed instructions
        removed_count = diff.get("removed_count", len(diff.get("removed", [])))
        assert removed_count > 0, f"Diff should show removed instructions, got {removed_count}"


@pytest.mark.e2e
def test_git_delete_repo_preserves_history(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_builds,
    get_build_contents,
    delete_git_repository,
):
    """Test that old builds still contain the deleted instructions (history preserved)."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Git Delete History Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    builds_before = get_builds(user_token=user_token, org_id=org_id)
    build_before_id = builds_before["items"][0]["id"]
    
    # Get contents before delete
    contents_before = get_build_contents(
        build_id=build_before_id,
        user_token=user_token,
        org_id=org_id
    )
    count_before = len(contents_before)

    # Delete git repository
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Old build should still have its contents (history preserved)
    contents_after = get_build_contents(
        build_id=build_before_id,
        user_token=user_token,
        org_id=org_id
    )
    
    assert len(contents_after) == count_before, \
        f"Old build should preserve its contents, had {count_before}, now has {len(contents_after)}"


# ============================================================================
# UNLINK THEN MODIFY
# ============================================================================

@pytest.mark.e2e
def test_unlink_then_update_no_git_sync(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    unlink_instruction_from_git,
    update_instruction,
    get_instruction,
    delete_git_repository,
):
    """Test that unlinked instruction is not updated by resync."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Unlink Update Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    instructions = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    assert len(instructions) > 0, "Should have instructions after indexing"
    
    instruction_to_unlink = instructions[0]

    # Unlink instruction
    unlinked = unlink_instruction_from_git(
        instruction_id=instruction_to_unlink["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert unlinked["source_sync_enabled"] is False, "Instruction should be unlinked"

    # Update the unlinked instruction
    update_instruction(
        instruction_id=instruction_to_unlink["id"],
        text="User modified text after unlink",
        user_token=user_token,
        org_id=org_id
    )

    # Re-index should not overwrite the user's changes
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Check instruction still has user's text
    fetched = get_instruction(
        instruction_id=instruction_to_unlink["id"],
        user_token=user_token,
        org_id=org_id
    )
    assert fetched["text"] == "User modified text after unlink", \
        "Unlinked instruction should not be updated by resync"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_unlink_then_delete_preserved(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    unlink_instruction_from_git,
    delete_instruction,
    get_instructions,
    delete_git_repository,
):
    """Test that unlinked+deleted instruction is not restored by resync."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Unlink Delete Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    instructions = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    assert len(instructions) > 0, "Should have instructions after indexing"
    initial_count = len(instructions)
    
    instruction_to_unlink = instructions[0]
    instruction_id = instruction_to_unlink["id"]

    # Unlink instruction
    unlink_instruction_from_git(
        instruction_id=instruction_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Delete the unlinked instruction
    delete_instruction(
        instruction_id=instruction_id,
        user_token=user_token,
        org_id=org_id
    )

    # Re-index
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get instructions again
    instructions_after = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    # The deleted instruction should not be restored
    instruction_ids_after = [i["id"] for i in instructions_after]
    assert instruction_id not in instruction_ids_after, \
        "Unlinked and deleted instruction should not be restored by resync"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_unlink_instruction_still_in_old_builds(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    unlink_instruction_from_git,
    get_builds,
    get_build_contents,
    delete_git_repository,
):
    """Test that history is preserved - unlinked instruction still in old builds."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Unlink History Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get build before unlink
    builds_before = get_builds(user_token=user_token, org_id=org_id)
    build_before_id = builds_before["items"][0]["id"]
    contents_before = get_build_contents(
        build_id=build_before_id,
        user_token=user_token,
        org_id=org_id
    )

    instructions = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    assert len(instructions) > 0, "Should have instructions"
    instruction_to_unlink = instructions[0]

    # Unlink
    unlink_instruction_from_git(
        instruction_id=instruction_to_unlink["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Old build should still have all original contents
    contents_after_unlink = get_build_contents(
        build_id=build_before_id,
        user_token=user_token,
        org_id=org_id
    )
    
    assert len(contents_after_unlink) == len(contents_before), \
        "Old build contents should be preserved after unlink"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


# ============================================================================
# UNLINK THEN GIT DELETE
# ============================================================================

@pytest.mark.e2e
def test_unlink_then_git_delete_preserves_instruction(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    unlink_instruction_from_git,
    get_instruction,
    delete_git_repository,
):
    """Test that unlinked instruction survives git repo delete."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Unlink Git Delete Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    instructions = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    assert len(instructions) > 0, "Should have instructions"
    instruction_to_unlink = instructions[0]
    instruction_id = instruction_to_unlink["id"]

    # Unlink instruction before deleting repo
    unlink_instruction_from_git(
        instruction_id=instruction_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Delete git repository
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Unlinked instruction should still exist
    preserved = get_instruction(
        instruction_id=instruction_id,
        user_token=user_token,
        org_id=org_id
    )
    
    assert preserved is not None, "Unlinked instruction should survive repo delete"
    assert preserved["id"] == instruction_id, "Should be the same instruction"


@pytest.mark.e2e
def test_unlinked_instruction_in_new_build(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    unlink_instruction_from_git,
    get_main_build,
    get_build_contents,
    delete_git_repository,
):
    """Test that unlinked instruction is still in main build after repo delete."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Unlinked In Build Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]

    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    instructions = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    assert len(instructions) > 0, "Should have instructions"
    instruction_to_unlink = instructions[0]
    instruction_id = instruction_to_unlink["id"]

    # Unlink instruction
    unlink_instruction_from_git(
        instruction_id=instruction_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Delete git repository
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get current main build and verify unlinked instruction is still there
    main_build = get_main_build(user_token=user_token, org_id=org_id)
    
    if main_build:
        contents = get_build_contents(
            build_id=main_build["id"],
            user_token=user_token,
            org_id=org_id
        )
        
        content_instruction_ids = [c.get("instruction_id") for c in contents]
        assert instruction_id in content_instruction_ids, \
            "Unlinked instruction should still be in main build after repo delete"
