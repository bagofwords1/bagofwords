from pathlib import Path

import pytest  # type: ignore

TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()
TEST_GIT_REPO_PATHS = ["https://github.com/bagofwords1/dbt-mock"]


@pytest.mark.e2e
@pytest.mark.parametrize("repo_url", TEST_GIT_REPO_PATHS)
def test_git_repository_create_index_delete(
    repo_url,
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    get_git_repository,
    index_git_repository,
    get_metadata_resources,
    get_instructions_by_source_type,
    delete_git_repository,
):
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Git Repo E2E",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": repo_url,
        "branch": "main",
        "is_active": True,
        "auto_publish": True,
        "default_load_mode": "always",
    }

    created_repo = create_git_repository(
        data_source_id=data_source["id"],
        payload=git_payload,
        user_token=user_token,
        org_id=org_id,
    )

    assert created_repo["repo_url"] == repo_url
    assert created_repo["provider"] == "github"
    # Verify new instruction sync settings
    assert created_repo.get("auto_publish") is True, "auto_publish should be True"
    assert created_repo.get("default_load_mode") == "always", "default_load_mode should be 'always'"
    repository_id = created_repo["id"]

    get_git_repository(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
    )


    reindex_response = index_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )
    assert reindex_response.get("status") == "success"

    metadata_resources = get_metadata_resources(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert metadata_resources["status"] == "completed"
    assert isinstance(metadata_resources.get("resources"), list)
    resources = metadata_resources.get("resources", [])
    assert len(resources) > 0, "Expected metadata resources after indexing"

    # Verify instructions were created from metadata resources
    # Fixture returns items directly (not paginated response)
    instructions = get_instructions_by_source_type(
        source_types=["git", "dbt", "markdown"],
        user_token=user_token,
        org_id=org_id,
        data_source_id=data_source["id"],
    )
    
    assert len(instructions) > 0, "Expected instructions to be created after indexing"
    
    # Verify instructions have correct properties based on git repo settings
    for instruction in instructions:
        assert instruction["source_type"] == "git", "Instruction should have source_type='git'"
        assert instruction["source_metadata_resource_id"] is not None, "Instruction should be linked to a resource"
        # Verify auto_publish=True results in published status
        assert instruction["status"] == "published", "Instruction should be published (auto_publish=True)"
        # Verify load_mode respects priority: frontmatter > git repo default_load_mode
        # Markdown files with alwaysApply: false in frontmatter will have 'intelligent'
        # Others should inherit 'always' from git repo's default_load_mode
        assert instruction["load_mode"] in ["always", "intelligent"], (
            f"Instruction should have valid load_mode, got {instruction.get('load_mode')}"
        )

    delete_response = delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )
    assert (
        delete_response["message"]
        == "Repository and associated data deleted successfully"
    )


@pytest.mark.e2e
@pytest.mark.parametrize("repo_url", TEST_GIT_REPO_PATHS)
def test_git_repository_update_metadata_resources(
    repo_url,
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_git_repository,
    index_git_repository,
    get_metadata_resources,
    update_metadata_resources,
    delete_git_repository,
):
    if not TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    data_source = create_data_source(
        name="Git Repo Metadata Update",
        type="sqlite",
        config={"database": str(TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    git_payload = {
        "provider": "github",
        "repo_url": repo_url,
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

    metadata_resources = get_metadata_resources(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
    )
    resources = metadata_resources.get("resources", [])
    assert resources, "Expected metadata resources after indexing"

    target_resource = resources[0]
    new_status = not target_resource.get("is_active", True)

    update_payload = [
        {
            "id": target_resource["id"],
            "is_active": new_status,
        }
    ]

    updated_response = update_metadata_resources(
        data_source_id=data_source["id"],
        resources=update_payload,
        user_token=user_token,
        org_id=org_id,
    )

    updated_resources = updated_response.get("resources", [])
    updated_target = next(
        (res for res in updated_resources if res["id"] == target_resource["id"]),
        None,
    )

    assert updated_target is not None
    assert updated_target["is_active"] == new_status

    delete_git_repository(
        data_source_id=data_source["id"],
        repository_id=repository_id,
        user_token=user_token,
        org_id=org_id,
    )