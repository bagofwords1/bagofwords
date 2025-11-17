import pytest
from fastapi.testclient import TestClient
from main import app
import os


@pytest.mark.e2e
def test_create_label_with_all_fields(create_label, create_user, login_user, whoami):
    """Test creating a label with name, color, and description."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    label = create_label(
        name="Finance",
        color="#34d399",
        description="Financial instructions",
        user_token=user_token,
        org_id=org_id
    )

    assert label is not None
    assert label["name"] == "Finance"
    assert label["color"] == "#34d399"
    assert label["description"] == "Financial instructions"
    assert label["organization_id"] == org_id
    assert label["created_by_user_id"] is not None


@pytest.mark.e2e
def test_create_label_minimal(create_label, create_user, login_user, whoami):
    """Test creating a label with only name (minimal required fields)."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    label = create_label(
        name="Marketing",
        user_token=user_token,
        org_id=org_id
    )

    assert label is not None
    assert label["name"] == "Marketing"
    assert label["color"] is not None  # Should have default or provided color
    assert label["organization_id"] == org_id


@pytest.mark.e2e
def test_create_label_duplicate_name_fails(create_label, create_user, login_user, whoami, test_client):
    """Test that creating a label with duplicate name (case-insensitive) fails."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create first label
    label1 = create_label(
        name="Finance",
        user_token=user_token,
        org_id=org_id
    )
    assert label1 is not None

    # Try to create duplicate (case-insensitive)
    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id)
    }
    response = test_client.post(
        "/api/instructions/labels",
        json={"name": "FINANCE", "color": "#34d399"},
        headers=headers
    )
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_create_label_empty_name_fails(create_user, login_user, whoami, test_client):
    """Test that creating a label with empty name fails."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id)
    }
    response = test_client.post(
        "/api/instructions/labels",
        json={"name": "", "color": "#34d399"},
        headers=headers
    )
    assert response.status_code == 400
    assert "required" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_list_labels(list_labels, create_label, create_user, login_user, whoami):
    """Test listing all labels for an organization."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create multiple labels
    label1 = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label2 = create_label(name="Marketing", user_token=user_token, org_id=org_id)
    label3 = create_label(name="Operations", user_token=user_token, org_id=org_id)

    # List labels
    labels = list_labels(user_token=user_token, org_id=org_id)

    assert len(labels) >= 3
    label_names = [l["name"] for l in labels]
    assert "Finance" in label_names
    assert "Marketing" in label_names
    assert "Operations" in label_names

    # Verify labels are sorted alphabetically (case-insensitive)
    label_names_lower = [l["name"].lower() for l in labels]
    assert label_names_lower == sorted(label_names_lower)


@pytest.mark.e2e
def test_list_labels_empty(list_labels, create_user, login_user, whoami):
    """Test listing labels when none exist returns empty list."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    labels = list_labels(user_token=user_token, org_id=org_id)
    assert isinstance(labels, list)
    assert len(labels) == 0


@pytest.mark.e2e
def test_update_label_name(update_label, create_label, create_user, login_user, whoami):
    """Test updating a label's name."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create label
    label = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label_id = label["id"]

    # Update name
    updated = update_label(
        label_id=label_id,
        name="Financial",
        user_token=user_token,
        org_id=org_id
    )

    assert updated["name"] == "Financial"
    assert updated["id"] == label_id
    assert updated["color"] == label["color"]  # Unchanged


@pytest.mark.e2e
def test_update_label_color(update_label, create_label, create_user, login_user, whoami):
    """Test updating a label's color."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create label
    label = create_label(name="Finance", color="#34d399", user_token=user_token, org_id=org_id)
    label_id = label["id"]

    # Update color
    updated = update_label(
        label_id=label_id,
        color="#f87171",
        user_token=user_token,
        org_id=org_id
    )

    assert updated["color"] == "#f87171"
    assert updated["name"] == "Finance"  # Unchanged


@pytest.mark.e2e
def test_update_label_description(update_label, create_label, create_user, login_user, whoami):
    """Test updating a label's description."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create label
    label = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label_id = label["id"]

    # Update description
    updated = update_label(
        label_id=label_id,
        description="Financial instructions",
        user_token=user_token,
        org_id=org_id
    )

    assert updated["description"] == "Financial instructions"
    assert updated["name"] == "Finance"  # Unchanged


@pytest.mark.e2e
def test_update_label_all_fields(update_label, create_label, create_user, login_user, whoami):
    """Test updating all fields of a label."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create label
    label = create_label(name="Finance", color="#34d399", user_token=user_token, org_id=org_id)
    label_id = label["id"]

    # Update all fields
    updated = update_label(
        label_id=label_id,
        name="Financial",
        color="#f87171",
        description="Updated financial instructions",
        user_token=user_token,
        org_id=org_id
    )

    assert updated["name"] == "Financial"
    assert updated["color"] == "#f87171"
    assert updated["description"] == "Updated financial instructions"


@pytest.mark.e2e
def test_update_label_same_name_allowed(update_label, create_label, create_user, login_user, whoami):
    """Test that updating a label to the same name (case-insensitive) is allowed."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create label
    label = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label_id = label["id"]

    # Update to same name (different case)
    updated = update_label(
        label_id=label_id,
        name="FINANCE",
        user_token=user_token,
        org_id=org_id
    )

    assert updated["name"] == "FINANCE"


@pytest.mark.e2e
def test_update_label_duplicate_name_fails(update_label, create_label, create_user, login_user, whoami, test_client):
    """Test that updating a label to duplicate another label's name fails."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create two labels
    label1 = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label2 = create_label(name="Marketing", user_token=user_token, org_id=org_id)

    # Try to update label2 to have same name as label1
    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id)
    }
    response = test_client.patch(
        f"/api/instructions/labels/{label2['id']}",
        json={"name": "Finance"},
        headers=headers
    )
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_update_label_not_found(update_label, create_user, login_user, whoami, test_client):
    """Test that updating a non-existent label returns 404."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id)
    }
    response = test_client.patch(
        "/api/instructions/labels/00000000-0000-0000-0000-000000000000",
        json={"name": "Test"},
        headers=headers
    )
    assert response.status_code == 404


@pytest.mark.e2e
def test_delete_label(delete_label, create_label, list_labels, create_user, login_user, whoami):
    """Test soft deleting a label."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create label
    label = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label_id = label["id"]

    # Verify it exists
    labels_before = list_labels(user_token=user_token, org_id=org_id)
    assert any(l["id"] == label_id for l in labels_before)

    # Delete label
    result = delete_label(label_id=label_id, user_token=user_token, org_id=org_id)
    assert result["message"] == "Label deleted successfully"

    # Verify it's gone from list
    labels_after = list_labels(user_token=user_token, org_id=org_id)
    assert not any(l["id"] == label_id for l in labels_after)


@pytest.mark.e2e
def test_delete_label_not_found(delete_label, create_user, login_user, whoami, test_client):
    """Test that deleting a non-existent label returns 404."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id)
    }
    response = test_client.delete(
        "/api/instructions/labels/00000000-0000-0000-0000-000000000000",
        headers=headers
    )
    assert response.status_code == 404


@pytest.mark.e2e
def test_delete_label_idempotent(delete_label, create_label, create_user, login_user, whoami):
    """Test that deleting an already-deleted label is idempotent."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create and delete label
    label = create_label(name="Finance", user_token=user_token, org_id=org_id)
    label_id = label["id"]

    result1 = delete_label(label_id=label_id, user_token=user_token, org_id=org_id)
    assert result1["message"] == "Label deleted successfully"

    # Delete again (should be idempotent)
    result2 = delete_label(label_id=label_id, user_token=user_token, org_id=org_id)
    assert result2["message"] == "Label deleted successfully"

