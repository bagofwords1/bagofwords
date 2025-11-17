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


@pytest.mark.e2e
def test_create_global_instruction_with_single_label(
    create_global_instruction,
    create_label,
    get_instruction,
    create_user,
    login_user,
    whoami
):
    """Test creating a global instruction with a single label."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create a label first
    label = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label_id = label["id"]

    # Create instruction with label
    instruction = create_global_instruction(
        text="A new global rule with label",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[label_id]
    )

    assert instruction is not None
    assert instruction["text"] == "A new global rule with label"
    assert len(instruction["labels"]) == 1
    assert instruction["labels"][0]["id"] == label_id
    assert instruction["labels"][0]["name"] == "Finance"

    # Verify by fetching the instruction
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert len(fetched["labels"]) == 1
    assert fetched["labels"][0]["id"] == label_id


@pytest.mark.e2e
def test_create_global_instruction_with_multiple_labels(
    create_global_instruction,
    create_label,
    get_instruction,
    create_user,
    login_user,
    whoami
):
    """Test creating a global instruction with multiple labels."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create multiple labels
    label1 = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label2 = create_label(name="Marketing", user_token=user_token, org_id=org_id)
    label3 = create_label(name="Operations", user_token=user_token, org_id=org_id)

    # Create instruction with multiple labels
    instruction = create_global_instruction(
        text="A new global rule with multiple labels",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[label1["id"], label2["id"], label3["id"]]
    )

    assert instruction is not None
    assert len(instruction["labels"]) == 3
    label_ids = [l["id"] for l in instruction["labels"]]
    assert label1["id"] in label_ids
    assert label2["id"] in label_ids
    assert label3["id"] in label_ids

    # Verify by fetching the instruction
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert len(fetched["labels"]) == 3


@pytest.mark.e2e
def test_create_global_instruction_without_labels(
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami
):
    """Test creating a global instruction without labels (empty array)."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create instruction without labels
    instruction = create_global_instruction(
        text="A new global rule without labels",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[]
    )

    assert instruction is not None
    assert instruction["labels"] == [] or len(instruction["labels"]) == 0

    # Verify by fetching the instruction
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert fetched["labels"] == [] or len(fetched["labels"]) == 0


@pytest.mark.e2e
def test_create_global_instruction_invalid_label_fails(
    create_global_instruction,
    create_user,
    login_user,
    whoami,
    test_client
):
    """Test that creating an instruction with invalid label IDs fails."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id)
    }
    payload = {
        "text": "Test instruction",
        "status": "published",
        "category": "general",
        "data_source_ids": [],
        "label_ids": ["00000000-0000-0000-0000-000000000000"]
    }

    response = test_client.post(
        "/api/instructions/global",
        json=payload,
        headers=headers
    )
    assert response.status_code == 400
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_update_instruction_add_labels(
    create_global_instruction,
    create_label,
    update_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami
):
    """Test adding labels to an instruction via update."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create instruction without labels
    instruction = create_global_instruction(
        text="Instruction without labels",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[]
    )
    assert len(instruction["labels"]) == 0

    # Create labels
    label1 = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label2 = create_label(name="Marketing", user_token=user_token, org_id=org_id)

    # Update instruction to add labels
    updated = update_instruction(
        instruction_id=instruction["id"],
        label_ids=[label1["id"], label2["id"]],
        user_token=user_token,
        org_id=org_id
    )

    assert len(updated["labels"]) == 2
    label_ids = [l["id"] for l in updated["labels"]]
    assert label1["id"] in label_ids
    assert label2["id"] in label_ids

    # Verify by fetching
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert len(fetched["labels"]) == 2


@pytest.mark.e2e
def test_update_instruction_replace_labels(
    create_global_instruction,
    create_label,
    update_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami
):
    """Test replacing labels on an instruction via update."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create labels
    label1 = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label2 = create_label(name="Marketing", user_token=user_token, org_id=org_id)
    label3 = create_label(name="Operations", user_token=user_token, org_id=org_id)

    # Create instruction with initial labels
    instruction = create_global_instruction(
        text="Instruction with initial labels",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[label1["id"], label2["id"]]
    )
    assert len(instruction["labels"]) == 2

    # Update instruction to replace labels
    updated = update_instruction(
        instruction_id=instruction["id"],
        label_ids=[label3["id"]],
        user_token=user_token,
        org_id=org_id
    )

    assert len(updated["labels"]) == 1
    assert updated["labels"][0]["id"] == label3["id"]

    # Verify by fetching
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert len(fetched["labels"]) == 1
    assert fetched["labels"][0]["id"] == label3["id"]


@pytest.mark.e2e
def test_update_instruction_remove_all_labels(
    create_global_instruction,
    create_label,
    update_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami
):
    """Test removing all labels from an instruction via update."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create label
    label = create_label(name="Finance", user_token=user_token, org_id=org_id)

    # Create instruction with label
    instruction = create_global_instruction(
        text="Instruction with label",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[label["id"]]
    )
    assert len(instruction["labels"]) == 1

    # Update instruction to remove all labels
    updated = update_instruction(
        instruction_id=instruction["id"],
        label_ids=[],
        user_token=user_token,
        org_id=org_id
    )

    assert len(updated["labels"]) == 0

    # Verify by fetching
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert len(fetched["labels"]) == 0


@pytest.mark.e2e
def test_update_instruction_invalid_label_fails(
    create_global_instruction,
    update_instruction,
    create_user,
    login_user,
    whoami,
    test_client
):
    """Test that updating an instruction with invalid label IDs fails."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create instruction
    instruction = create_global_instruction(
        text="Test instruction",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    # Try to update with invalid label
    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id)
    }
    response = test_client.put(
        f"/api/instructions/{instruction['id']}",
        json={"label_ids": ["00000000-0000-0000-0000-000000000000"]},
        headers=headers
    )
    assert response.status_code == 400
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_delete_instruction_with_labels(
    create_global_instruction,
    create_label,
    delete_instruction,
    list_labels,
    create_user,
    login_user,
    whoami
):
    """Test that deleting an instruction removes label associations but labels remain."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create label
    label = create_label(name="Finance", user_token=user_token, org_id=org_id)

    # Create instruction with label
    instruction = create_global_instruction(
        text="Instruction to delete",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[label["id"]]
    )
    assert len(instruction["labels"]) == 1

    # Delete instruction
    result = delete_instruction(
        instruction_id=instruction["id"],
        user_token=user_token,
        org_id=org_id
    )
    assert result["message"] == "Instruction deleted successfully"

    # Verify label still exists
    labels = list_labels(user_token=user_token, org_id=org_id)
    assert any(l["id"] == label["id"] for l in labels)


@pytest.mark.e2e
def test_multiple_instructions_share_label(
    create_global_instruction,
    create_label,
    get_instructions,
    create_user,
    login_user,
    whoami
):
    """Test that multiple instructions can share the same label."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create a shared label
    shared_label = create_label(name="Shared Label", user_token=user_token, org_id=org_id)

    # Create multiple instructions with the same label
    instruction1 = create_global_instruction(
        text="First instruction",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[shared_label["id"]]
    )
    instruction2 = create_global_instruction(
        text="Second instruction",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[shared_label["id"]]
    )
    instruction3 = create_global_instruction(
        text="Third instruction",
        user_token=user_token,
        org_id=org_id,
        status="published",
        label_ids=[shared_label["id"]]
    )

    # Verify all instructions have the label
    assert len(instruction1["labels"]) == 1
    assert instruction1["labels"][0]["id"] == shared_label["id"]
    assert len(instruction2["labels"]) == 1
    assert instruction2["labels"][0]["id"] == shared_label["id"]
    assert len(instruction3["labels"]) == 1
    assert instruction3["labels"][0]["id"] == shared_label["id"]
