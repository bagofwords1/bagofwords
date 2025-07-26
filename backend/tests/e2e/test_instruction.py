import pytest

@pytest.mark.e2e
def test_global_instruction_creation(create_global_instruction_fixture, admin_user_token, organization_id):
    """Tests that an admin can create a global instruction."""
    instruction = create_global_instruction_fixture(
        text="A new global rule",
        user_token=admin_user_token,
        org_id=organization_id,
        status="published"
    )

    assert instruction is not None
    assert instruction["text"] == "A new global rule"
    assert instruction["status"] == "published"
    assert instruction["private_status"] is None
    assert instruction["global_status"] == "approved"