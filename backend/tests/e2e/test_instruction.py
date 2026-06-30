import pytest
from fastapi.testclient import TestClient
from main import app
import os

@pytest.mark.e2e
def test_global_instruction_creation(create_global_instruction,
get_instruction,
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
    # Note: private_status and global_status are deprecated - approval workflow is on builds
    
    # Build System: Verify version is created
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert fetched.get("current_version_id") is not None, "Instruction should have current_version_id after creation"


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

    # Get original version ID
    original = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    original_version_id = original.get("current_version_id")

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
    
    # Build System: Verify version changed after label update
    new_version_id = fetched.get("current_version_id")
    assert new_version_id is not None, "Should have current_version_id"
    assert new_version_id != original_version_id, "Version should change after adding labels"


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

    # Get original version ID
    original = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    original_version_id = original.get("current_version_id")

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
    
    # Build System: Verify new version created after label replacement
    new_version_id = fetched.get("current_version_id")
    assert new_version_id is not None, "Should have current_version_id"
    assert new_version_id != original_version_id, "New version should be created after replacing labels"


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


# ============================================================================
# BUILD SYSTEM INTEGRATION TESTS
# ============================================================================

@pytest.mark.e2e
def test_create_instruction_verify_build_exists(
    create_global_instruction,
    get_main_build,
    get_build_contents,
    create_user,
    login_user,
    whoami
):
    """Test that created instruction is in the main build contents."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create instruction
    instruction = create_global_instruction(
        text="Instruction for build verification",
        user_token=user_token,
        org_id=org_id,
        status="published"
    )

    # Verify main build exists
    main_build = get_main_build(user_token=user_token, org_id=org_id)
    assert main_build is not None, "Main build should exist after instruction creation"
    
    # Verify instruction is in build contents
    contents = get_build_contents(
        build_id=main_build["id"],
        user_token=user_token,
        org_id=org_id
    )
    
    instruction_ids_in_build = [c.get("instruction_id") for c in contents]
    assert instruction["id"] in instruction_ids_in_build, \
        "Created instruction should be in the main build contents"


@pytest.mark.e2e
def test_create_instruction_returns_503_when_finalize_fails(
    test_client, create_user, login_user, whoami, monkeypatch
):
    """If the build can't be promoted (e.g. a concurrent transaction holds the
    instruction_builds lock until lock_timeout fires), a synchronous create must
    fail fast with a clean, retryable 503 — not hang, not 500, and not leave a
    half-created instruction stranded out of the main build.

    Regression test for the Postgres build-promotion deadlock fix. We simulate
    the finalize failure directly (the real trigger is lock contention, which a
    sequential test can't produce) by forcing _auto_finalize_build to report
    failure.
    """
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    from app.routes.instruction import instruction_service

    async def _finalize_fails(db, build, current_user, user_permissions):
        # Mimic the real failure path: roll back the poisoned transaction and
        # report that promotion did not happen.
        await db.rollback()
        return False

    monkeypatch.setattr(instruction_service, "_auto_finalize_build", _finalize_fails)

    resp = test_client.post(
        "/api/instructions/global",
        json={"text": "finalize-fails-503", "status": "published", "category": "general"},
        headers=headers,
    )
    assert resp.status_code == 503, resp.text

    # No orphan: the half-created instruction must not be visible in the list.
    list_resp = test_client.get(
        "/api/instructions", params={"search": "finalize-fails-503"}, headers=headers
    )
    assert list_resp.status_code == 200, list_resp.text
    body = list_resp.json()
    items = body["items"] if isinstance(body, dict) and "items" in body else body
    assert not any(i["text"] == "finalize-fails-503" for i in items), \
        "failed create must not leave a visible orphan instruction"


@pytest.mark.e2e
def test_instruction_applicable_modes_and_channels(
    create_global_instruction,
    get_instruction,
    update_instruction,
    create_user,
    login_user,
    whoami,
):
    """Instructions can be scoped to specific run-modes and delivery channels.

    Covers create round-trip, GET persistence, admin update, and that an edit
    to these fields produces a new version (build-system integration)."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    # Create with explicit modes/channels
    instruction = create_global_instruction(
        text="Scoped to chat + slack",
        user_token=user_token,
        org_id=org_id,
        status="published",
        applicable_modes=["chat", "deep"],
        applicable_channels=["slack", "app"],
    )
    assert instruction["applicable_modes"] == ["chat", "deep"]
    assert instruction["applicable_channels"] == ["slack", "app"]
    v1 = instruction.get("current_version_id")
    assert v1 is not None

    # Persisted on GET
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert fetched["applicable_modes"] == ["chat", "deep"]
    assert fetched["applicable_channels"] == ["slack", "app"]

    # Update (admin edit) narrows the scope and clears channels (empty = all)
    updated = update_instruction(
        instruction["id"],
        applicable_modes=["training"],
        applicable_channels=[],
        user_token=user_token,
        org_id=org_id,
    )
    assert updated["applicable_modes"] == ["training"]
    assert updated["applicable_channels"] == []

    refetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert refetched["applicable_modes"] == ["training"]
    assert refetched["applicable_channels"] == []
    # Editing these versioned fields must produce a new version.
    assert refetched.get("current_version_id") not in (None, v1), \
        "changing applicable_modes/channels should create a new version"


@pytest.mark.e2e
def test_instruction_modes_channels_default_to_all(
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami,
):
    """Omitting the fields leaves them null = applies to all modes/channels."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Applies everywhere",
        user_token=user_token,
        org_id=org_id,
        status="published",
    )
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    # Null (or empty) means "applies everywhere"
    assert not fetched.get("applicable_modes")
    assert not fetched.get("applicable_channels")


@pytest.mark.e2e
def test_pending_status_consistent_between_list_and_detail(
    create_global_instruction,
    get_instruction,
    get_instructions,
    create_user,
    login_user,
    whoami,
):
    """Regression: status must not disagree across views.

    A leftover/covered pending build (its version differs from main but its
    change is already applied — i.e. a no-op with no LIVE review hunk) must NOT
    read as 'Pending review' in the list while the detail reads 'Active'. Both
    the list and the single-instruction GET now derive the pending signal from
    the same authoritative per-hunk rule as /instructions/pending-changes.
    """
    import os, uuid, datetime
    from sqlalchemy import create_engine, text

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    instr = create_global_instruction(
        text="Consistency check — alpha beta gamma",
        user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]

    # Inject a covered/no-op leftover pending build straight into the test DB.
    # Use SQLAlchemy with named params + Python-typed values so this runs on
    # both the sqlite and postgres matrix legs (normalize the async driver to a
    # sync one; pass real bools/datetimes rather than `1`/ISO strings).
    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            main = conn.execute(
                text(
                    """SELECT bc.build_id AS build_id, iv.text AS text
                         FROM build_contents bc
                         JOIN instruction_builds ib ON ib.id = bc.build_id
                         JOIN instruction_versions iv ON iv.id = bc.instruction_version_id
                        WHERE bc.instruction_id = :iid AND ib.is_main = :is_main
                          AND ib.deleted_at IS NULL"""
                ),
                {"iid": iid, "is_main": True},
            ).mappings().fetchone()
            assert main is not None, "new instruction should be in the main build"
            now = datetime.datetime.utcnow()
            vid, bid, bcid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            vnum = conn.execute(
                text("SELECT COALESCE(MAX(version_number),0)+1 FROM instruction_versions WHERE instruction_id=:iid"),
                {"iid": iid},
            ).scalar()
            bnum = conn.execute(
                text("SELECT COALESCE(MAX(build_number),0)+1 FROM instruction_builds WHERE organization_id=:org"),
                {"org": org_id},
            ).scalar()
            # Same text as main => covered/no-op, but a distinct version id.
            conn.execute(
                text(
                    "INSERT INTO instruction_versions (id,created_at,updated_at,instruction_id,version_number,text,status,load_mode,content_hash)"
                    " VALUES (:id,:ca,:ua,:iid,:vnum,:txt,:status,:load_mode,:chash)"
                ),
                {"id": vid, "ca": now, "ua": now, "iid": iid, "vnum": vnum, "txt": main["text"],
                 "status": "published", "load_mode": "always", "chash": "h" + uuid.uuid4().hex[:12]},
            )
            conn.execute(
                text(
                    "INSERT INTO instruction_builds (id,created_at,updated_at,build_number,status,source,is_main,organization_id,base_build_id,title)"
                    " VALUES (:id,:ca,:ua,:bnum,:status,:source,:is_main,:org,:base,:title)"
                ),
                {"id": bid, "ca": now, "ua": now, "bnum": bnum, "status": "pending_approval", "source": "ai",
                 "is_main": False, "org": org_id, "base": main["build_id"], "title": "leftover covered build"},
            )
            conn.execute(
                text(
                    "INSERT INTO build_contents (id,created_at,updated_at,build_id,instruction_id,instruction_version_id)"
                    " VALUES (:id,:ca,:ua,:bid,:iid,:vid)"
                ),
                {"id": bcid, "ca": now, "ua": now, "bid": bid, "iid": iid, "vid": vid},
            )
    finally:
        engine.dispose()

    def effective(o):
        if o.get("current_build_id") and o.get("current_build_status") in ("draft", "pending_approval"):
            return "pending_review"
        return o["status"]

    single = get_instruction(iid, user_token=token, org_id=org_id)
    row = next(x for x in get_instructions(user_token=token, org_id=org_id) if x["id"] == iid)

    # The covered build is a no-op vs main -> neither surface should flag pending,
    # and they must agree with each other.
    assert effective(single) == "published"
    assert effective(row) == "published", "list must not show pending when detail shows Active"
    assert effective(single) == effective(row)


@pytest.mark.e2e
def test_agent_count_matches_list_under_inaccessible_table(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Regression for the /agents tree 3->0 flicker.

    An agent's Instructions node first renders the badge count
    (GET /api/instructions/counts -> by_agent[agent]) then switches to the
    per-agent lazy list length once it loads. When a table-pinned instruction's
    only referenced table is in the user's per-user inaccessible-table overlay,
    the list filters it out (_filter_list_items_by_table_accessibility) — so the
    counts MUST apply the same cut, otherwise the badge counts an instruction the
    list then drops and the node flickers 3 -> 0 / "No instructions yet".
    """
    import os, uuid, datetime
    from sqlalchemy import create_engine, text

    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    user_id = me["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    ds_id = str(uuid.uuid4())
    table_id = str(uuid.uuid4())

    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    now = datetime.datetime.utcnow()
    try:
        with engine.begin() as conn:
            # Agent (data source) the user is a member of, with one active table.
            conn.execute(
                text(
                    "INSERT INTO data_sources (id,created_at,updated_at,name,is_active,organization_id,"
                    "is_public,use_llm_sync,publish_status,reliability_status)"
                    " VALUES (:id,:ca,:ua,:name,:active,:org,:pub,:llm,:pubst,:rel)"
                ),
                {"id": ds_id, "ca": now, "ua": now, "name": "Pinned Agent", "active": True,
                 "org": org_id, "pub": False, "llm": False, "pubst": "published", "rel": "unknown"},
            )
            conn.execute(
                text(
                    "INSERT INTO data_source_memberships (id,created_at,updated_at,data_source_id,principal_type,principal_id,config)"
                    " VALUES (:id,:ca,:ua,:ds,:pt,:pid,:cfg)"
                ),
                {"id": str(uuid.uuid4()), "ca": now, "ua": now, "ds": ds_id,
                 "pt": "user", "pid": user_id, "cfg": '{"role": "owner"}'},
            )
            conn.execute(
                text(
                    "INSERT INTO datasource_tables (id,created_at,updated_at,name,datasource_id,is_active,columns,no_rows,pks,fks)"
                    " VALUES (:id,:ca,:ua,:name,:ds,:active,:cols,:nr,:pks,:fks)"
                ),
                {"id": table_id, "ca": now, "ua": now, "name": "orders", "ds": ds_id, "active": True,
                 "cols": "[]", "nr": 0, "pks": "[]", "fks": "[]"},
            )
    finally:
        engine.dispose()

    # Create an instruction PINNED to that table via the real API (so it gets a
    # main-build version and a datasource_table reference through the service).
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Pinned instruction for orders",
            "status": "published",
            "category": "general",
            "data_source_ids": [ds_id],
            "references": [{
                "object_type": "datasource_table",
                "object_id": table_id,
                "display_text": "orders",
            }],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    def by_agent_count():
        r = test_client.get("/api/instructions/counts", headers=headers)
        assert r.status_code == 200, r.json()
        return r.json()["by_agent"].get(ds_id, 0)

    def list_count():
        r = test_client.get(
            "/api/instructions",
            params={"data_source_ids": ds_id, "include_global": "false", "limit": 200,
                    "include_own": "true", "include_drafts": "true", "include_archived": "true"},
            headers=headers,
        )
        assert r.status_code == 200, r.json()
        return len(r.json()["items"])

    # Table accessible (no overlay): badge and list agree at 1.
    assert by_agent_count() == 1
    assert list_count() == 1
    assert by_agent_count() == list_count()

    # Mark the table inaccessible for this user. Now the list hides the
    # instruction; the count must match (no 3->0 flicker).
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO user_data_source_tables (id,created_at,updated_at,data_source_id,user_id,"
                    "table_name,data_source_table_id,is_accessible,status)"
                    " VALUES (:id,:ca,:ua,:ds,:uid,:tn,:tid,:acc,:st)"
                ),
                {"id": str(uuid.uuid4()), "ca": now, "ua": now, "ds": ds_id, "uid": user_id,
                 "tn": "orders", "tid": table_id, "acc": False, "st": "inaccessible"},
            )
    finally:
        engine.dispose()

    assert list_count() == 0, "list must hide an instruction whose only table is inaccessible"
    assert by_agent_count() == 0, "badge count must match the list (no 3->0 flicker)"
    assert by_agent_count() == list_count()
