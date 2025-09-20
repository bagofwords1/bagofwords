import pytest
from fastapi.testclient import TestClient
from main import app
import os

@pytest.mark.e2e
def test_global_instruction_creation(create_global_instruction,
create_user,
login_user,
whoami):

    """Tests that an admin can create a global instruction."""


    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    instruction = create_global_instruction(
        text="A new global rule",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    assert instruction is not None
    assert instruction["text"] == "A new global rule"
    assert instruction["status"] == "published"
    assert instruction["private_status"] is None
    assert instruction["global_status"] == "approved"


@pytest.mark.e2e
def test_get_instructions(get_instructions, create_global_instruction,
create_user,
login_user,
whoami):

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    instruction1 = create_global_instruction(
        text="A new global rule 1",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    instruction2 = create_global_instruction(
        text="A new global rule 2",
        user_token=user_token,
        org_id=org_id,
        status="draft"
    )

    instruction3 = create_global_instruction(
        text="A new global rule 3",
        user_token=user_token,
        org_id=org_id,
        status="archived"
    )

    instructions = get_instructions(user_token=user_token, org_id=org_id)

    assert len(instructions) == 3
    assert instructions[2]["text"] == "A new global rule 1"
    assert instructions[1]["text"] == "A new global rule 2"
    assert instructions[0]["text"] == "A new global rule 3"
    assert instructions[0]["status"] == "archived"
    assert instructions[1]["status"] == "draft"
    assert instructions[2]["status"] == "published"