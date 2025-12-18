"""
E2E tests for Instruction Sync Service.

Tests the synchronization of git-indexed MetadataResources to Instructions,
including auto_publish settings, default_load_mode, and unlink behavior.
"""
from pathlib import Path

import pytest  # type: ignore

TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()
TEST_GIT_REPO_URL = "https://github.com/bagofwords1/dbt-mock"


@pytest.mark.e2e
def test_git_indexing_creates_instructions(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_metadata_resources,
    get_instructions_by_source_type,
    delete_git_repository,
):
    """Test that indexing a git repository creates instructions for each resource."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create data source
    data_source = create_data_source(
        name="Instruction Sync Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create git repository with default settings
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

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get metadata resources to know how many we expect
    metadata_resources = get_metadata_resources(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
    )
    resources = metadata_resources.get("resources", [])
    assert len(resources) > 0, "Expected metadata resources after indexing"

    # Get git-sourced instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    # Handle paginated response
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) > 0, "Expected instructions to be created after indexing"

    # Verify instruction properties
    for instruction in instructions:
        assert instruction["source_type"] == "git", "Instruction should have source_type='git'"
        assert instruction["source_metadata_resource_id"] is not None, "Instruction should be linked to a resource"
        assert instruction["source_sync_enabled"] is True, "Instruction should be synced"
        assert instruction["title"] is not None, "Instruction should have a title"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_auto_publish_true_creates_published_instructions(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    delete_git_repository,
):
    """Test that auto_publish=True creates published instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Auto Publish Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create git repository with auto_publish=True
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
    
    # Verify auto_publish was saved
    assert created_repo.get("auto_publish") is True, "auto_publish should be True"

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get git-sourced instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) > 0, "Expected instructions after indexing"

    # Verify all instructions are published
    for instruction in instructions:
        assert instruction["status"] == "published", f"Instruction should be published, got {instruction['status']}"
        assert instruction["global_status"] == "approved", f"Instruction should be approved, got {instruction.get('global_status')}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_auto_publish_false_creates_draft_instructions(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    delete_git_repository,
):
    """Test that auto_publish=False (default) creates draft instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Draft Instructions Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create git repository with auto_publish=False (explicit)
    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
        "auto_publish": False,
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]
    
    # Verify auto_publish was saved
    assert created_repo.get("auto_publish") is False, "auto_publish should be False"

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get git-sourced instructions (include drafts)
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) > 0, "Expected instructions after indexing"

    # Verify all instructions are draft
    for instruction in instructions:
        assert instruction["status"] == "draft", f"Instruction should be draft, got {instruction['status']}"
        assert instruction.get("global_status") is None, f"Instruction should have no global_status, got {instruction.get('global_status')}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_default_load_mode_applied_to_instructions(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    delete_git_repository,
):
    """Test that default_load_mode from git repository is applied to instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Load Mode Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create git repository with default_load_mode='always'
    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
        "default_load_mode": "always",
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )
    repository_id = created_repo["id"]
    
    # Verify default_load_mode was saved
    assert created_repo.get("default_load_mode") == "always", "default_load_mode should be 'always'"

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get git-sourced instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) > 0, "Expected instructions after indexing"

    # Verify load_mode is set to 'always'
    for instruction in instructions:
        assert instruction["load_mode"] == "always", f"Instruction should have load_mode='always', got {instruction.get('load_mode')}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_unlink_instruction_preserves_on_delete(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    get_instruction,
    unlink_instruction_from_git,
    delete_git_repository,
):
    """Test that unlinked instructions are preserved when git repository is deleted."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Unlink Preserve Test",
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

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get git-sourced instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) >= 2, "Need at least 2 instructions for this test"

    # Unlink one instruction
    instruction_to_unlink = instructions[0]
    unlinked = unlink_instruction_from_git(
        instruction_id=instruction_to_unlink["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert unlinked["source_sync_enabled"] is False, "Instruction should be unlinked"

    # Delete git repository
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Verify the unlinked instruction still exists
    preserved = get_instruction(
        instruction_id=instruction_to_unlink["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert preserved is not None, "Unlinked instruction should be preserved"
    assert preserved["id"] == instruction_to_unlink["id"]


@pytest.mark.e2e
def test_delete_git_repo_deletes_synced_instructions(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    delete_git_repository,
):
    """Test that deleting a git repository deletes all synced instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Delete Sync Test",
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

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Verify instructions exist
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    initial_count = len(instructions)
    assert initial_count > 0, "Expected instructions after indexing"

    # Delete git repository
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Verify instructions are deleted
    instructions_after = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    remaining = instructions_after.get("items", instructions_after)
    if isinstance(remaining, dict):
        remaining = remaining.get("items", [])
    
    assert len(remaining) == 0, f"Expected no instructions after repo deletion, found {len(remaining)}"


@pytest.mark.e2e
def test_linked_instruction_count_excludes_unlinked(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    get_linked_instructions_count,
    unlink_instruction_from_git,
    delete_git_repository,
):
    """Test that linked instruction count excludes unlinked instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Count Test",
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

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Get initial linked count
    initial_count_response = get_linked_instructions_count(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )
    initial_count = initial_count_response.get("instruction_count", 0)
    assert initial_count > 0, "Expected linked instructions"

    # Get an instruction to unlink
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) > 0, "Need instructions to unlink"

    # Unlink one instruction
    instruction_to_unlink = instructions[0]
    unlink_instruction_from_git(
        instruction_id=instruction_to_unlink["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Get updated linked count
    updated_count_response = get_linked_instructions_count(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )
    updated_count = updated_count_response.get("instruction_count", 0)
    
    # Count should decrease by 1
    assert updated_count == initial_count - 1, f"Expected count to decrease by 1: {initial_count} -> {updated_count}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


# ============================================================================
# BULK UPDATE TESTS
# ============================================================================

@pytest.mark.e2e
def test_bulk_update_status_to_published(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    get_instruction,
    bulk_update_instructions,
    delete_git_repository,
):
    """Test bulk updating instruction status from draft to published."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Bulk Status Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create git repo with auto_publish=False to get draft instructions
    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
        "auto_publish": False,
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

    # Get draft instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) >= 2, "Need at least 2 instructions for bulk test"
    
    # Verify they're drafts
    for inst in instructions[:2]:
        assert inst["status"] == "draft", f"Expected draft, got {inst['status']}"

    # Bulk update to published
    instruction_ids = [inst["id"] for inst in instructions[:2]]
    result = bulk_update_instructions(
        ids=instruction_ids,
        status="published",
        user_token=user_token,
        org_id=org_id,
    )
    
    assert result["updated_count"] == 2, f"Expected 2 updated, got {result['updated_count']}"
    assert len(result.get("failed_ids", [])) == 0, "Expected no failures"

    # Verify instructions are now published
    for inst_id in instruction_ids:
        updated_inst = get_instruction(
            instruction_id=inst_id,
            user_token=user_token,
            org_id=org_id,
        )
        assert updated_inst["status"] == "published", f"Expected published, got {updated_inst['status']}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_bulk_update_load_mode_to_always(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    get_instruction,
    bulk_update_instructions,
    delete_git_repository,
):
    """Test bulk updating instruction load_mode to 'always'."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Bulk Load Mode Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create git repo with default_load_mode='intelligent'
    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
        "default_load_mode": "intelligent",
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

    # Get instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) >= 2, "Need at least 2 instructions for bulk test"

    # Bulk update to load_mode='always'
    instruction_ids = [inst["id"] for inst in instructions[:2]]
    result = bulk_update_instructions(
        ids=instruction_ids,
        load_mode="always",
        user_token=user_token,
        org_id=org_id,
    )
    
    assert result["updated_count"] == 2, f"Expected 2 updated, got {result['updated_count']}"

    # Verify instructions have load_mode='always'
    for inst_id in instruction_ids:
        updated_inst = get_instruction(
            instruction_id=inst_id,
            user_token=user_token,
            org_id=org_id,
        )
        assert updated_inst["load_mode"] == "always", f"Expected 'always', got {updated_inst.get('load_mode')}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_bulk_add_label_to_instructions(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    get_instruction,
    create_label,
    bulk_update_instructions,
    delete_git_repository,
):
    """Test bulk adding a label to multiple instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Bulk Label Test",
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

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Create a label
    label = create_label(
        name="DBT Models",
        color="#10B981",
        user_token=user_token,
        org_id=org_id,
    )
    label_id = label["id"]

    # Get instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) >= 2, "Need at least 2 instructions for bulk test"

    # Verify instructions don't have the label yet
    for inst in instructions[:2]:
        label_ids = [l["id"] for l in inst.get("labels", [])]
        assert label_id not in label_ids, "Instruction should not have the label yet"

    # Bulk add label
    instruction_ids = [inst["id"] for inst in instructions[:2]]
    result = bulk_update_instructions(
        ids=instruction_ids,
        add_label_ids=[label_id],
        user_token=user_token,
        org_id=org_id,
    )
    
    assert result["updated_count"] == 2, f"Expected 2 updated, got {result['updated_count']}"

    # Verify instructions now have the label
    for inst_id in instruction_ids:
        updated_inst = get_instruction(
            instruction_id=inst_id,
            user_token=user_token,
            org_id=org_id,
        )
        label_ids = [l["id"] for l in updated_inst.get("labels", [])]
        assert label_id in label_ids, f"Instruction should have the label, got {label_ids}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_bulk_remove_label_from_instructions(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    get_instruction,
    create_label,
    bulk_update_instructions,
    delete_git_repository,
):
    """Test bulk removing a label from multiple instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Bulk Remove Label Test",
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

    # Index the repository
    index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )

    # Create a label
    label = create_label(
        name="To Remove",
        color="#EF4444",
        user_token=user_token,
        org_id=org_id,
    )
    label_id = label["id"]

    # Get instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) >= 2, "Need at least 2 instructions for bulk test"

    # First, bulk add the label
    instruction_ids = [inst["id"] for inst in instructions[:2]]
    bulk_update_instructions(
        ids=instruction_ids,
        add_label_ids=[label_id],
        user_token=user_token,
        org_id=org_id,
    )

    # Verify label was added
    for inst_id in instruction_ids:
        updated_inst = get_instruction(
            instruction_id=inst_id,
            user_token=user_token,
            org_id=org_id,
        )
        label_ids = [l["id"] for l in updated_inst.get("labels", [])]
        assert label_id in label_ids, "Label should have been added"

    # Now bulk remove the label
    result = bulk_update_instructions(
        ids=instruction_ids,
        remove_label_ids=[label_id],
        user_token=user_token,
        org_id=org_id,
    )
    
    assert result["updated_count"] == 2, f"Expected 2 updated, got {result['updated_count']}"

    # Verify label was removed
    for inst_id in instruction_ids:
        updated_inst = get_instruction(
            instruction_id=inst_id,
            user_token=user_token,
            org_id=org_id,
        )
        label_ids = [l["id"] for l in updated_inst.get("labels", [])]
        assert label_id not in label_ids, f"Label should have been removed, got {label_ids}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_bulk_update_combined_status_and_load_mode(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    get_instruction,
    bulk_update_instructions,
    delete_git_repository,
):
    """Test bulk updating both status and load_mode in a single operation."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Bulk Combined Test",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create git repo with auto_publish=False and default_load_mode='intelligent'
    git_payload = {
        "provider": "github",
        "repo_url": TEST_GIT_REPO_URL,
        "branch": "main",
        "is_active": True,
        "auto_publish": False,
        "default_load_mode": "intelligent",
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

    # Get instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) >= 2, "Need at least 2 instructions for bulk test"
    
    # Verify initial state
    for inst in instructions[:2]:
        assert inst["status"] == "draft"
        assert inst["load_mode"] == "intelligent"

    # Bulk update both status and load_mode
    instruction_ids = [inst["id"] for inst in instructions[:2]]
    result = bulk_update_instructions(
        ids=instruction_ids,
        status="published",
        load_mode="always",
        user_token=user_token,
        org_id=org_id,
    )
    
    assert result["updated_count"] == 2, f"Expected 2 updated, got {result['updated_count']}"

    # Verify both fields were updated
    for inst_id in instruction_ids:
        updated_inst = get_instruction(
            instruction_id=inst_id,
            user_token=user_token,
            org_id=org_id,
        )
        assert updated_inst["status"] == "published", f"Expected published, got {updated_inst['status']}"
        assert updated_inst["load_mode"] == "always", f"Expected 'always', got {updated_inst.get('load_mode')}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_bulk_archive_instructions(
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_instructions_by_source_type,
    get_instruction,
    bulk_update_instructions,
    delete_git_repository,
):
    """Test bulk archiving instructions."""
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Bulk Archive Test",
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
        "auto_publish": True,  # Start with published instructions
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

    # Get instructions
    instructions_response = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    instructions = instructions_response.get("items", instructions_response)
    if isinstance(instructions, dict):
        instructions = instructions.get("items", [])
    
    assert len(instructions) >= 2, "Need at least 2 instructions for bulk test"

    # Bulk archive
    instruction_ids = [inst["id"] for inst in instructions[:2]]
    result = bulk_update_instructions(
        ids=instruction_ids,
        status="archived",
        user_token=user_token,
        org_id=org_id,
    )
    
    assert result["updated_count"] == 2, f"Expected 2 updated, got {result['updated_count']}"

    # Verify instructions are archived
    for inst_id in instruction_ids:
        updated_inst = get_instruction(
            instruction_id=inst_id,
            user_token=user_token,
            org_id=org_id,
        )
        assert updated_inst["status"] == "archived", f"Expected archived, got {updated_inst['status']}"

    # Cleanup
    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )
