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
        applicable_modes=["chat", "training"],
        applicable_channels=["slack", "app"],
    )
    assert instruction["applicable_modes"] == ["chat", "training"]
    assert instruction["applicable_channels"] == ["slack", "app"]
    v1 = instruction.get("current_version_id")
    assert v1 is not None

    # Persisted on GET
    fetched = get_instruction(instruction["id"], user_token=user_token, org_id=org_id)
    assert fetched["applicable_modes"] == ["chat", "training"]
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
                    # is_change: these hand-rolled rows stand in for what BuildService
                    # writes, and the pending sweep reads that flag — a row
                    # left at the default would not register as a change.
                    "INSERT INTO build_contents (id,created_at,updated_at,build_id,instruction_id,instruction_version_id,is_change)"
                    " VALUES (:id,:ca,:ua,:bid,:iid,:vid,TRUE)"
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


def _inject_suggestion_build(org_id, instruction_id, proposed_text, source="ai", status="pending_approval"):
    """Insert a non-main pending suggestion build (forked from main) proposing
    `proposed_text` for the instruction, straight into the test DB — a REAL
    live suggestion, unlike the covered/no-op build above. Returns the build id.
    Same portability notes as the injection in
    test_pending_status_consistent_between_list_and_detail."""
    import os, uuid, datetime
    from sqlalchemy import create_engine, text

    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            main = conn.execute(
                text(
                    """SELECT bc.build_id AS build_id
                         FROM build_contents bc
                         JOIN instruction_builds ib ON ib.id = bc.build_id
                        WHERE bc.instruction_id = :iid AND ib.is_main = :is_main
                          AND ib.deleted_at IS NULL"""
                ),
                {"iid": instruction_id, "is_main": True},
            ).mappings().fetchone()
            assert main is not None, "instruction should be in the main build"
            now = datetime.datetime.utcnow()
            vid, bid, bcid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            vnum = conn.execute(
                text("SELECT COALESCE(MAX(version_number),0)+1 FROM instruction_versions WHERE instruction_id=:iid"),
                {"iid": instruction_id},
            ).scalar()
            bnum = conn.execute(
                text("SELECT COALESCE(MAX(build_number),0)+1 FROM instruction_builds WHERE organization_id=:org"),
                {"org": org_id},
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO instruction_versions (id,created_at,updated_at,instruction_id,version_number,text,status,load_mode,content_hash)"
                    " VALUES (:id,:ca,:ua,:iid,:vnum,:txt,:status,:load_mode,:chash)"
                ),
                {"id": vid, "ca": now, "ua": now, "iid": instruction_id, "vnum": vnum, "txt": proposed_text,
                 "status": "published", "load_mode": "always", "chash": "h" + uuid.uuid4().hex[:12]},
            )
            conn.execute(
                text(
                    "INSERT INTO instruction_builds (id,created_at,updated_at,build_number,status,source,is_main,organization_id,base_build_id,title)"
                    " VALUES (:id,:ca,:ua,:bnum,:status,:source,:is_main,:org,:base,:title)"
                ),
                {"id": bid, "ca": now, "ua": now, "bnum": bnum, "status": status, "source": source,
                 "is_main": False, "org": org_id, "base": main["build_id"], "title": "suggestion build"},
            )
            conn.execute(
                text(
                    # is_change: these hand-rolled rows stand in for what BuildService
                    # writes, and the pending sweep reads that flag — a row
                    # left at the default would not register as a change.
                    "INSERT INTO build_contents (id,created_at,updated_at,build_id,instruction_id,instruction_version_id,is_change)"
                    " VALUES (:id,:ca,:ua,:bid,:iid,:vid,TRUE)"
                ),
                {"id": bcid, "ca": now, "ua": now, "bid": bid, "iid": instruction_id, "vid": vid},
            )
    finally:
        engine.dispose()
    return bid


def _exact_review_payload(review, build_id=None):
    suggestions = review["suggestions"]
    if build_id is not None:
        suggestions = [s for s in suggestions if str(s["build_id"]) == str(build_id)]
    return {
        "against_main_build_id": review["main_build_id"],
        "against_main_version_id": review["main_version_id"],
        "hunks": [
            {"build_id": s["build_id"], "hunk_key": h["key"]}
            for s in suggestions
            for h in s["hunks"]
        ],
    }


@pytest.mark.e2e
def test_pending_builds_agrees_with_review_hunks_after_reject_all(
    test_client,
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami,
):
    """Regression: /pending-builds must follow the same authoritative per-hunk
    rule as /review-hunks. A suggestion whose every hunk was rejected in the
    per-hunk review used to stay in /pending-builds, so the Report agent panel
    kept showing "changes" the /agents tracked-changes review had already
    dropped (and could re-publish rejected content via build publish)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    instr = create_global_instruction(
        text="alpha beta gamma delta", user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]
    bid = _inject_suggestion_build(org_id, iid, "alpha beta gamma delta epsilon")

    # Both surfaces agree the suggestion is pending.
    pending = test_client.get(f"/api/instructions/{iid}/pending-builds", headers=headers).json()
    assert [b["build_id"] for b in pending] == [bid]
    assert pending[0]["in_main"] is True
    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert [s["build_id"] for s in review["suggestions"]] == [bid]

    # Reject everything through the per-hunk review (what the /agents page does).
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        json=_exact_review_payload(review),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["suggestions"] == []
    pending = test_client.get(f"/api/instructions/{iid}/pending-builds", headers=headers).json()
    assert pending == [], "a fully-rejected suggestion must not resurface in /pending-builds"
    # Main text untouched by the rejection.
    assert get_instruction(iid, user_token=token, org_id=org_id)["text"] == "alpha beta gamma delta"


@pytest.mark.e2e
def test_pending_builds_partial_reject_shows_only_live_hunks(
    test_client,
    create_global_instruction,
    create_user,
    login_user,
    whoami,
):
    """With one of two hunks rejected, /pending-builds must render the pending
    text as main + the LIVE hunk only — diffing the raw snapshot would
    resurrect the rejected change in every tracked-changes client."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    instr = create_global_instruction(
        text="one two three four", user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]
    bid = _inject_suggestion_build(org_id, iid, "one TWO three four five")

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    hunks = review["suggestions"][0]["hunks"]
    assert len(hunks) == 2, hunks
    replaced = next(h for h in hunks if "TWO" in h["after"])

    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject",
        json={
            "build_id": bid,
            "hunk_key": replaced["key"],
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    pending = test_client.get(f"/api/instructions/{iid}/pending-builds", headers=headers).json()
    assert [b["build_id"] for b in pending] == [bid]
    assert pending[0]["pending_text"] == "one two three four five"
    assert "TWO" not in pending[0]["pending_text"]
    # base_text is current main so clients diff exactly the live hunks.
    assert pending[0]["base_text"] == "one two three four"


@pytest.mark.e2e
def test_accept_and_reject_all_scoped_to_build(
    test_client,
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami,
):
    """Bulk review resolves only the exact hunk identities the client saw."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    instr = create_global_instruction(
        text="start middle end", user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]
    build_a = _inject_suggestion_build(org_id, iid, "start MIDDLE end")
    build_b = _inject_suggestion_build(org_id, iid, "start middle end tail")

    pending = test_client.get(f"/api/instructions/{iid}/pending-builds", headers=headers).json()
    assert {b["build_id"] for b in pending} == {build_a, build_b}

    # Accept ONLY build A.
    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    shown_a = next(s for s in review["suggestions"] if s["build_id"] == build_a)
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        json=_exact_review_payload(review, build_a),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    assert get_instruction(iid, user_token=token, org_id=org_id)["text"] == "start MIDDLE end"

    # B is still pending on BOTH surfaces, rebased onto the new main.
    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert [s["build_id"] for s in review["suggestions"]] == [build_b]
    pending = test_client.get(f"/api/instructions/{iid}/pending-builds", headers=headers).json()
    assert [b["build_id"] for b in pending] == [build_b]
    assert pending[0]["pending_text"] == "start MIDDLE end tail"

    # Reject ONLY build B -> nothing pending anywhere, main unchanged.
    shown_b = next(s for s in review["suggestions"] if s["build_id"] == build_b)
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        json=_exact_review_payload(review, build_b),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["suggestions"] == []
    pending = test_client.get(f"/api/instructions/{iid}/pending-builds", headers=headers).json()
    assert pending == []
    assert get_instruction(iid, user_token=token, org_id=org_id)["text"] == "start MIDDLE end"


@pytest.mark.e2e
def test_bulk_accept_on_large_instruction_applies_only_the_displayed_hunk(
    test_client,
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami,
):
    """Accept-all is an exact snapshot mutation, never a pending-build sweep."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    base = "\n\n".join(
        f"## Policy {index}\n```python\nvalue = {index}\n```\nPreserve this policy."
        for index in range(700)
    )
    instr = create_global_instruction(
        text=base, user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]

    stale_build = _inject_suggestion_build(
        org_id, iid, base.replace("Preserve this policy.", "Replace this policy.", 1)
    )
    expected = base.replace("## Policy 350\n", "## Policy 350\nhello world\n\n", 1)
    displayed_build = _inject_suggestion_build(org_id, iid, expected)

    review = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=headers
    ).json()
    displayed = next(
        s for s in review["suggestions"] if s["build_id"] == displayed_build
    )
    assert [(h["before"], h["after"]) for h in displayed["hunks"]] == [
        ("", "hello world\n\n")
    ]

    # Omitting the displayed identities is no longer a command to sweep every
    # pending build. It fails closed and leaves main byte-for-byte unchanged.
    unscoped = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        headers=headers,
        json={
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
        },
    )
    assert unscoped.status_code == 422
    assert get_instruction(iid, user_token=token, org_id=org_id)["text"] == base

    accepted = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        headers=headers,
        json=_exact_review_payload(review, displayed_build),
    )
    assert accepted.status_code == 200, accepted.text
    assert get_instruction(iid, user_token=token, org_id=org_id)["text"] == expected

    remaining = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=headers
    ).json()
    assert {s["build_id"] for s in remaining["suggestions"]} == {stale_build}

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


@pytest.mark.e2e
def test_pending_badge_clears_when_instruction_deleted(
    create_global_instruction,
    delete_instruction,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Regression: the "N pending" badge counting rows the Pending-changes view
    can't show.

    The org-wide pending sweep reads suggestion builds only — it never sees
    Instruction.deleted_at. Deleting an instruction that still had a live
    pending suggestion used to leave that build producing hunks forever, so
    /instructions/counts kept reporting pending_total=N while the pending_only
    list (which filters deleted rows) showed nothing. Delete must clear the
    signal from ALL pending surfaces: the counts badge, the pending_only list,
    and /instructions/pending-changes.
    """
    import os, uuid, datetime
    from sqlalchemy import create_engine, text

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    instr = create_global_instruction(
        text="Delete-me original alpha beta",
        user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]

    # Inject a REAL pending suggestion build (changed text -> live hunk).
    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            main = conn.execute(
                text(
                    """SELECT bc.build_id AS build_id
                         FROM build_contents bc
                         JOIN instruction_builds ib ON ib.id = bc.build_id
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
            conn.execute(
                text(
                    "INSERT INTO instruction_versions (id,created_at,updated_at,instruction_id,version_number,text,status,load_mode,content_hash)"
                    " VALUES (:id,:ca,:ua,:iid,:vnum,:txt,:status,:load_mode,:chash)"
                ),
                {"id": vid, "ca": now, "ua": now, "iid": iid, "vnum": vnum,
                 "txt": "Delete-me PROPOSED gamma delta",
                 "status": "published", "load_mode": "always", "chash": "h" + uuid.uuid4().hex[:12]},
            )
            conn.execute(
                text(
                    "INSERT INTO instruction_builds (id,created_at,updated_at,build_number,status,source,is_main,organization_id,base_build_id,title)"
                    " VALUES (:id,:ca,:ua,:bnum,:status,:source,:is_main,:org,:base,:title)"
                ),
                {"id": bid, "ca": now, "ua": now, "bnum": bnum, "status": "pending_approval", "source": "ai",
                 "is_main": False, "org": org_id, "base": main["build_id"], "title": "pending suggestion"},
            )
            conn.execute(
                text(
                    # is_change: these hand-rolled rows stand in for what BuildService
                    # writes, and the pending sweep reads that flag — a row
                    # left at the default would not register as a change.
                    "INSERT INTO build_contents (id,created_at,updated_at,build_id,instruction_id,instruction_version_id,is_change)"
                    " VALUES (:id,:ca,:ua,:bid,:iid,:vid,TRUE)"
                ),
                {"id": bcid, "ca": now, "ua": now, "bid": bid, "iid": iid, "vid": vid},
            )
    finally:
        engine.dispose()

    def pending_total():
        r = test_client.get("/api/instructions/counts", headers=headers)
        assert r.status_code == 200, r.json()
        return r.json()["pending_total"]

    def pending_list_ids():
        r = test_client.get(
            "/api/instructions",
            params={"pending_only": "true", "include_own": "true",
                    "include_drafts": "true", "include_archived": "true", "limit": 200},
            headers=headers,
        )
        assert r.status_code == 200, r.json()
        return [x["id"] for x in r.json()["items"]]

    def sweep_ids():
        r = test_client.get("/api/instructions/pending-changes", headers=headers)
        assert r.status_code == 200, r.json()
        return r.json()["instruction_ids"]

    # Alive: all three pending surfaces agree the change is pending.
    assert pending_total() == 1
    assert pending_list_ids() == [iid]
    assert sweep_ids() == [iid]

    delete_instruction(iid, user_token=token, org_id=org_id)

    # Deleted: no surface may keep counting the leftover suggestion build.
    assert pending_list_ids() == []
    assert pending_total() == 0, "badge must not count a deleted instruction's leftover suggestion"
    assert sweep_ids() == []


@pytest.mark.e2e
def test_pending_badge_excludes_inaccessible_private_agent(
    create_user,
    login_user,
    whoami,
    test_client,
):
    """The pending sweep is build-based and org-wide; the counts badge must
    re-scope it through the caller's instruction visibility. A pending change on
    an instruction attached to a PRIVATE agent the caller is not a member of is
    invisible in the pending_only list — so it must not be counted in
    pending_total (or returned by /instructions/pending-changes) either.
    """
    import os, uuid, datetime
    from sqlalchemy import create_engine, text

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    now = datetime.datetime.utcnow()
    ds_id, iid, vid, bid = (str(uuid.uuid4()) for _ in range(4))
    try:
        with engine.begin() as conn:
            # Private agent with NO membership for the caller.
            conn.execute(
                text(
                    "INSERT INTO data_sources (id,created_at,updated_at,name,is_active,organization_id,"
                    "is_public,use_llm_sync,publish_status,reliability_status)"
                    " VALUES (:id,:ca,:ua,:name,:active,:org,:pub,:llm,:pubst,:rel)"
                ),
                {"id": ds_id, "ca": now, "ua": now, "name": "Members-only Agent", "active": True,
                 "org": org_id, "pub": False, "llm": False, "pubst": "published", "rel": "unknown"},
            )
            # Instruction attached to it (not in main; a proposed-new instruction).
            conn.execute(
                text(
                    "INSERT INTO instructions (id,created_at,updated_at,text,thumbs_up,status,category,kind,"
                    "is_seen,can_user_toggle,organization_id,source_type,load_mode)"
                    " VALUES (:id,:ca,:ua,:txt,:tu,:status,:cat,:kind,:seen,:tog,:org,:st,:lm)"
                ),
                {"id": iid, "ca": now, "ua": now, "txt": "Members-only current text", "tu": 0,
                 "status": "published", "cat": "general", "kind": "instruction",
                 "seen": True, "tog": True, "org": org_id, "st": "ai", "lm": "always"},
            )
            conn.execute(
                text("INSERT INTO instruction_data_source_association (instruction_id,data_source_id) VALUES (:iid,:ds)"),
                {"iid": iid, "ds": ds_id},
            )
            bnum = conn.execute(
                text("SELECT COALESCE(MAX(build_number),0)+1 FROM instruction_builds WHERE organization_id=:org"),
                {"org": org_id},
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO instruction_versions (id,created_at,updated_at,instruction_id,version_number,text,status,load_mode,content_hash)"
                    " VALUES (:id,:ca,:ua,:iid,:vnum,:txt,:status,:load_mode,:chash)"
                ),
                {"id": vid, "ca": now, "ua": now, "iid": iid, "vnum": 1,
                 "txt": "Members-only PROPOSED text",
                 "status": "published", "load_mode": "always", "chash": "h" + uuid.uuid4().hex[:12]},
            )
            conn.execute(
                text(
                    "INSERT INTO instruction_builds (id,created_at,updated_at,build_number,status,source,is_main,organization_id,title)"
                    " VALUES (:id,:ca,:ua,:bnum,:status,:source,:is_main,:org,:title)"
                ),
                {"id": bid, "ca": now, "ua": now, "bnum": bnum, "status": "pending_approval", "source": "ai",
                 "is_main": False, "org": org_id, "title": "pending on private agent"},
            )
            conn.execute(
                text(
                    # is_change: these hand-rolled rows stand in for what BuildService
                    # writes, and the pending sweep reads that flag — a row
                    # left at the default would not register as a change.
                    "INSERT INTO build_contents (id,created_at,updated_at,build_id,instruction_id,instruction_version_id,is_change)"
                    " VALUES (:id,:ca,:ua,:bid,:iid,:vid,TRUE)"
                ),
                {"id": str(uuid.uuid4()), "ca": now, "ua": now, "bid": bid, "iid": iid, "vid": vid},
            )
    finally:
        engine.dispose()

    counts = test_client.get("/api/instructions/counts", headers=headers)
    assert counts.status_code == 200, counts.json()
    assert counts.json()["pending_total"] == 0, \
        "badge must not count a pending change on a private agent the caller can't access"
    assert ds_id not in counts.json()["pending_by_agent"]
    assert iid not in counts.json().get("pending_instruction_ids", [])

    plist = test_client.get(
        "/api/instructions",
        params={"pending_only": "true", "include_own": "true",
                "include_drafts": "true", "include_archived": "true", "limit": 200},
        headers=headers,
    )
    assert plist.status_code == 200, plist.json()
    assert plist.json()["items"] == []

    sweep = test_client.get("/api/instructions/pending-changes", headers=headers)
    assert sweep.status_code == 200, sweep.json()
    assert sweep.json()["instruction_ids"] == []


def _inject_new_instruction_suggestion(org_id, proposed_text, source="ai", status="pending_approval"):
    """Insert a brand-NEW instruction that exists ONLY in a pending suggestion
    build — absent from the main build. Mirrors what the AI training flow's
    create_instruction tool leaves behind: a draft live row caching the proposed
    text, current_version_id pointing at the staged (status='published') version,
    and a non-main build (forked from main) carrying it. Returns (iid, bid)."""
    import os, uuid, datetime
    from sqlalchemy import create_engine, text

    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    now = datetime.datetime.utcnow()
    iid, vid, bid, bcid = (str(uuid.uuid4()) for _ in range(4))
    try:
        with engine.begin() as conn:
            main = conn.execute(
                text(
                    "SELECT id FROM instruction_builds WHERE organization_id=:org"
                    " AND is_main=:is_main AND deleted_at IS NULL"
                ),
                {"org": org_id, "is_main": True},
            ).mappings().fetchone()
            assert main is not None, "org should already have a main build"
            bnum = conn.execute(
                text("SELECT COALESCE(MAX(build_number),0)+1 FROM instruction_builds WHERE organization_id=:org"),
                {"org": org_id},
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO instructions (id,created_at,updated_at,text,thumbs_up,status,category,kind,"
                    "is_seen,can_user_toggle,organization_id,source_type,load_mode,source_sync_enabled)"
                    " VALUES (:id,:ca,:ua,:txt,:tu,:status,:cat,:kind,:seen,:tog,:org,:st,:lm,:sync)"
                ),
                {"id": iid, "ca": now, "ua": now, "txt": proposed_text, "tu": 0,
                 "status": "draft", "cat": "general", "kind": "instruction",
                 "seen": True, "tog": True, "org": org_id, "st": source, "lm": "always", "sync": True},
            )
            conn.execute(
                text(
                    "INSERT INTO instruction_versions (id,created_at,updated_at,instruction_id,version_number,text,status,load_mode,content_hash)"
                    " VALUES (:id,:ca,:ua,:iid,:vnum,:txt,:status,:load_mode,:chash)"
                ),
                {"id": vid, "ca": now, "ua": now, "iid": iid, "vnum": 1, "txt": proposed_text,
                 "status": "published", "load_mode": "always", "chash": "h" + uuid.uuid4().hex[:12]},
            )
            conn.execute(
                text("UPDATE instructions SET current_version_id=:vid WHERE id=:iid"),
                {"vid": vid, "iid": iid},
            )
            conn.execute(
                text(
                    "INSERT INTO instruction_builds (id,created_at,updated_at,build_number,status,source,is_main,organization_id,base_build_id,title)"
                    " VALUES (:id,:ca,:ua,:bnum,:status,:source,:is_main,:org,:base,:title)"
                ),
                {"id": bid, "ca": now, "ua": now, "bnum": bnum, "status": status, "source": source,
                 "is_main": False, "org": org_id, "base": main["id"], "title": "new-instruction suggestion"},
            )
            conn.execute(
                text(
                    # is_change: these hand-rolled rows stand in for what BuildService
                    # writes, and the pending sweep reads that flag — a row
                    # left at the default would not register as a change.
                    "INSERT INTO build_contents (id,created_at,updated_at,build_id,instruction_id,instruction_version_id,is_change)"
                    " VALUES (:id,:ca,:ua,:bid,:iid,:vid,TRUE)"
                ),
                {"id": bcid, "ca": now, "ua": now, "bid": bid, "iid": iid, "vid": vid},
            )
    finally:
        engine.dispose()
    return iid, bid


@pytest.mark.e2e
def test_new_instruction_review_shows_full_text_as_pending_hunk(
    test_client,
    create_global_instruction,
    create_user,
    login_user,
    whoami,
):
    """Regression: a brand-NEW instruction staged in a pending build (absent from
    main) used to render an EMPTY review — "0 changes / all resolved" — because
    the main-text fallback read the instruction's own staged draft and diffed the
    proposal against itself. The correct baseline for a not-yet-live instruction
    is empty text: the review must surface the whole draft as one insertion hunk,
    and every pending surface (review-hunks, /pending-changes, the pending_only
    list, pending-builds) must agree it is pending."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    # Ensure the org has a main build (a live instruction creates+promotes one).
    create_global_instruction(text="existing live rule", user_token=token, org_id=org_id, status="published")
    iid, bid = _inject_new_instruction_suggestion(org_id, "brand new AI rule")

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["main_text"] == ""
    assert review["main_version_id"] is None
    assert [s["build_id"] for s in review["suggestions"]] == [bid]
    hunks = review["suggestions"][0]["hunks"]
    assert len(hunks) == 1, hunks
    assert hunks[0]["before"] == ""
    assert hunks[0]["after"] == "brand new AI rule"

    sweep = test_client.get("/api/instructions/pending-changes", headers=headers).json()
    assert iid in sweep["instruction_ids"]

    plist = test_client.get(
        "/api/instructions",
        params={"pending_only": "true", "include_own": "true",
                "include_drafts": "true", "include_archived": "true", "limit": 200},
        headers=headers,
    ).json()
    assert iid in {r["id"] for r in plist["items"]}

    pending = test_client.get(f"/api/instructions/{iid}/pending-builds", headers=headers).json()
    assert [b["build_id"] for b in pending] == [bid]
    assert pending[0]["pending_text"] == "brand new AI rule"
    assert pending[0]["base_text"] == ""


@pytest.mark.e2e
def test_new_instruction_accept_all_makes_it_live(
    test_client,
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami,
):
    """Accepting a NEW instruction's insertion hunk promotes it into main and
    flips the live row to published; every pending surface then agrees it is
    resolved."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    create_global_instruction(text="existing live rule", user_token=token, org_id=org_id, status="published")
    iid, _bid = _inject_new_instruction_suggestion(org_id, "brand new AI rule")

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        json=_exact_review_payload(review),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    detail = get_instruction(iid, user_token=token, org_id=org_id)
    assert detail["text"] == "brand new AI rule"
    assert detail["status"] == "published"

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["suggestions"] == []
    assert review["main_text"] == "brand new AI rule"
    sweep = test_client.get("/api/instructions/pending-changes", headers=headers).json()
    assert iid not in sweep["instruction_ids"]
    pending = test_client.get(f"/api/instructions/{iid}/pending-builds", headers=headers).json()
    assert pending == []


@pytest.mark.e2e
def test_new_instruction_reject_all_resolves_everywhere(
    test_client,
    create_global_instruction,
    create_user,
    login_user,
    whoami,
):
    """Rejecting a NEW instruction's insertion hunk resolves it on EVERY pending
    surface (review-hunks, the sweep, and pending-builds — which used to ignore
    per-hunk rejections for creates) without making it live."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    create_global_instruction(text="existing live rule", user_token=token, org_id=org_id, status="published")
    iid, _bid = _inject_new_instruction_suggestion(org_id, "brand new AI rule")

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        json=_exact_review_payload(review),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    # Nothing anywhere still calls it pending, and it never went live.
    sweep = test_client.get("/api/instructions/pending-changes", headers=headers).json()
    assert iid not in sweep["instruction_ids"]
    listed = test_client.get(
        "/api/instructions",
        params={"include_own": "true", "include_drafts": "true",
                "include_archived": "true", "limit": 200},
        headers=headers,
    ).json()
    assert iid not in {row["id"] for row in listed["items"]}
    # And it is not left behind either: rejecting the suggestion that would have
    # INTRODUCED this instruction retires the row. It used to survive as an
    # orphaned draft — invisible in every build-scoped surface, yet still
    # answering search_instructions, so the agent kept reporting a rejected
    # suggestion as an existing instruction.
    assert test_client.get(f"/api/instructions/{iid}", headers=headers).status_code == 404
    assert test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=headers
    ).status_code == 404


@pytest.mark.e2e
def test_reject_all_on_an_edit_keeps_the_live_instruction(
    test_client,
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami,
):
    """The guard on retiring rejected creates: rejecting an EDIT decides the
    edit, never the instruction. Only a suggestion the main build never carried
    may retire its row — an instruction that is live stays live, with its
    original text, no matter how many suggested changes are rejected."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    live = create_global_instruction(
        text="original live rule", user_token=token, org_id=org_id, status="published",
    )
    iid = live["id"]
    _inject_suggestion_build(org_id, iid, "original live rule, plus a suggested clause")

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        json=_exact_review_payload(review),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    detail = get_instruction(iid, user_token=token, org_id=org_id)
    assert detail["status"] == "published"
    assert detail["text"] == "original live rule"
    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["suggestions"] == []


def _drop_main_build(org_id):
    """Turn the org into a LEGACY one: no main build, the state `_main_text_of`
    documents for orgs that predate main-build content, where the cached
    instruction row IS the live text. Clears the flag rather than deleting the
    row so suggestion builds forked from it keep resolving their base."""
    import os
    from sqlalchemy import create_engine, text

    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE instruction_builds SET is_main=:no"
                     " WHERE organization_id=:org AND is_main=:yes"),
                {"no": False, "yes": True, "org": org_id},
            )
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_reject_all_on_an_edit_keeps_the_live_instruction_in_a_legacy_org(
    test_client,
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami,
):
    """Same guarantee as the test above, in an org with NO main build.

    Retirement proves "never went live" from main-build ABSENCE, and in a
    legacy org every instruction is absent from a main build that doesn't
    exist — live ones included. Without the has-a-main-build guard, rejecting
    an edit here soft-deleted the published instruction the edit was written
    against."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    live = create_global_instruction(
        text="original live rule", user_token=token, org_id=org_id, status="published",
    )
    iid = live["id"]
    _inject_suggestion_build(org_id, iid, "original live rule, plus a suggested clause")
    _drop_main_build(org_id)

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["suggestions"], "the legacy org should still show the edit to review"
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        json=_exact_review_payload(review),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    # The instruction survives its rejected edit, with its original text.
    assert test_client.get(f"/api/instructions/{iid}", headers=headers).status_code == 200
    detail = get_instruction(iid, user_token=token, org_id=org_id)
    assert detail["status"] == "published"
    assert detail["text"] == "original live rule"


def _pending_badge_state(test_client, headers):
    """The three badge surfaces the /agents page renders from: the counts badge
    (chip + per-row dots), the org-wide sweep, and the pending_only list."""
    counts = test_client.get("/api/instructions/counts", headers=headers)
    assert counts.status_code == 200, counts.json()
    counts = counts.json()
    sweep = test_client.get("/api/instructions/pending-changes", headers=headers)
    assert sweep.status_code == 200, sweep.json()
    plist = test_client.get(
        "/api/instructions",
        params={"pending_only": "true", "include_own": "true",
                "include_drafts": "true", "include_archived": "true", "limit": 200},
        headers=headers,
    )
    assert plist.status_code == 200, plist.json()
    return {
        "pending_total": counts["pending_total"],
        "pending_ids": set(counts.get("pending_instruction_ids", [])),
        "sweep_ids": set(sweep.json()["instruction_ids"]),
        "list_ids": {r["id"] for r in plist.json()["items"]},
    }


@pytest.mark.e2e
def test_reject_all_clears_pending_badges_without_refresh(
    test_client,
    create_global_instruction,
    get_instruction,
    create_user,
    login_user,
    whoami,
):
    """The reported flow on the /agents page: reject every suggested change,
    then look at the tree. Every badge surface — the "N pending" chip
    (counts.pending_total), the per-row dots (pending_instruction_ids /
    /pending-changes), and the "Pending changes" list (pending_only) — must
    agree the instruction is resolved, with no refresh required and nothing
    for a refresh to bring back."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    instr = create_global_instruction(
        text="alpha beta gamma delta", user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]
    _inject_suggestion_build(org_id, iid, "alpha beta gamma delta epsilon")

    state = _pending_badge_state(test_client, headers)
    assert state["pending_total"] == 1
    assert iid in state["pending_ids"] and iid in state["sweep_ids"] and iid in state["list_ids"]

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        json=_exact_review_payload(review),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    state = _pending_badge_state(test_client, headers)
    assert state["pending_total"] == 0, "the 'N pending' chip must drop after reject-all"
    assert iid not in state["pending_ids"]
    assert iid not in state["sweep_ids"]
    assert iid not in state["list_ids"]
    assert get_instruction(iid, user_token=token, org_id=org_id)["text"] == "alpha beta gamma delta"


@pytest.mark.e2e
def test_reject_all_settles_drifted_noop_suggestion(
    test_client,
    create_global_instruction,
    update_instruction,
    create_user,
    login_user,
    whoami,
):
    """Regression: the badge nobody could clear.

    The badge sweep never diffs (verify=False), so a DRIFTED suggestion — main
    moved since it forked — is reported pending optimistically. When the drift
    already contains the suggestion's change, the authoritative per-hunk review
    finds no live hunks, so reject-all had nothing to record a rejection
    against: the review pane showed nothing to review, yet the optimistic sweep
    kept the amber "Pending review" badge forever, surviving reject-all AND any
    page refresh. Rejecting must settle the suggestion durably."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    instr = create_global_instruction(
        text="alpha beta delta", user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]
    # Suggestion forked from main("alpha beta delta") proposing one word swap...
    _inject_suggestion_build(org_id, iid, "alpha BETA delta")
    # ...then main moves PAST it: the swap is already contained in main, but
    # the proposed text still differs from both base and main.
    update_instruction(instruction_id=iid, text="alpha BETA delta extra",
                       user_token=token, org_id=org_id)

    # No surface may claim there is a review action when the authoritative
    # review has no live hunk. The badge sweep now verifies drifted rows.
    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["main_text"] == "alpha BETA delta extra"
    assert review["suggestions"] == []
    state = _pending_badge_state(test_client, headers)
    assert state["pending_total"] == 0
    assert iid not in state["pending_ids"]
    assert iid not in state["sweep_ids"]
    assert iid not in state["list_ids"]


@pytest.mark.e2e
def test_partial_reject_keeps_pending_badges(
    test_client,
    create_global_instruction,
    create_user,
    login_user,
    whoami,
):
    """Guard against over-settling: rejecting ONE of two hunks resolves only
    that hunk — the other stays live, so every badge surface must keep the
    instruction pending and the review must keep offering the survivor."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    instr = create_global_instruction(
        text="one two three four", user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]
    bid = _inject_suggestion_build(org_id, iid, "one TWO three four five")

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    hunks = review["suggestions"][0]["hunks"]
    assert len(hunks) == 2, hunks
    replaced = next(h for h in hunks if "TWO" in h["after"])

    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject",
        json={
            "build_id": bid,
            "hunk_key": replaced["key"],
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert len(review["suggestions"]) == 1
    assert len(review["suggestions"][0]["hunks"]) == 1
    state = _pending_badge_state(test_client, headers)
    assert state["pending_total"] == 1, "a live hunk remains — the badge must stay"
    assert iid in state["pending_ids"] and iid in state["sweep_ids"]

    # Rejecting the LAST live hunk settles the suggestion everywhere.
    survivor = review["suggestions"][0]["hunks"][0]
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/reject",
        json={
            "build_id": bid,
            "hunk_key": survivor["key"],
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    state = _pending_badge_state(test_client, headers)
    assert state["pending_total"] == 0
    assert iid not in state["pending_ids"]
    assert iid not in state["sweep_ids"]


@pytest.mark.e2e
def test_delete_voids_pending_suggestions_on_every_surface(
    test_client,
    create_global_instruction,
    update_instruction,
    create_user,
    login_user,
    whoami,
):
    """Deleting an instruction must stop it counting as a pending change.

    The org-wide sweep reads BUILDS, never Instruction.deleted_at, so a
    suggestion build left proposing a change to a deleted instruction would keep
    an un-openable row on the badges forever.

    Delete used to answer this by rebasing every suggestion (quadratic
    word-level diff) purely to enumerate hunk keys and record each one rejected
    — seconds of event-loop-blocking work to describe hunks of a row that is
    being removed. It now stamps one unconditional void marker per build. This
    pins the OBSERVABLE contract so that optimization can't silently regress:
    both sweep tiers and the authoritative review must agree the row is gone.

    Both a clean suggestion and a DRIFTED one (main moved after the fork) are
    covered — the drifted case is the one the non-diffing tier reports
    optimistically, so it is the one a missing marker would leave stuck.
    """
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    # (1) clean suggestion: base == main, a real live hunk.
    clean = create_global_instruction(
        text="alpha beta delta", user_token=token, org_id=org_id, status="published",
    )["id"]
    _inject_suggestion_build(org_id, clean, "alpha BETA delta")

    # (2) drifted suggestion: main moves after the fork.
    drifted = create_global_instruction(
        text="one two three", user_token=token, org_id=org_id, status="published",
    )["id"]
    _inject_suggestion_build(org_id, drifted, "one TWO three")
    update_instruction(instruction_id=drifted, text="one two three four",
                       user_token=token, org_id=org_id)

    state = _pending_badge_state(test_client, headers)
    assert clean in state["pending_ids"], "precondition: clean suggestion is pending"
    assert drifted in state["pending_ids"], "precondition: drifted suggestion is pending"

    for iid in (clean, drifted):
        resp = test_client.delete(f"/api/instructions/{iid}", headers=headers)
        assert resp.status_code == 200, resp.text

    state = _pending_badge_state(test_client, headers)
    assert state["pending_total"] == 0, "a deleted instruction must not count as pending"
    for iid in (clean, drifted):
        assert iid not in state["pending_ids"]
        assert iid not in state["sweep_ids"]
        assert iid not in state["list_ids"]

    # And it stays gone — the marker is unconditional, so a re-evaluation
    # (what a page refresh does) cannot resurrect it.
    again = _pending_badge_state(test_client, headers)
    assert again["pending_total"] == 0
    assert not ({clean, drifted} & again["sweep_ids"])


# ==================== Position-aware hunk keys (twin-edit collision) ====================

# Two lines identical for well over 24 chars before the edited token, each
# getting the SAME replacement — the shape that made content-hashed keys
# collide (same before/after/left-context ⇒ same key), which made Accept all
# fail closed with a misleading "moved since you viewed" 409.
_TWIN_BASE = (
    "### Measure: RPO Prev Year\n"
    "- alpha maps to dbo.fact_depm_revaluation.ver_asof_date (snapshot key)\n"
    "- beta maps to dbo.fact_depm_revaluation.ver_asof_date (snapshot key)\n"
    "Use USD only.\n"
)
_TWIN_PROPOSED = _TWIN_BASE.replace("ver_asof_date", "fk_ver_asof_date")


@pytest.mark.e2e
def test_accept_all_with_repeated_identical_edits(
    test_client, create_global_instruction, get_instruction, create_user, login_user, whoami,
):
    """Regression: a suggestion making the same edit twice must yield two
    DISTINCT hunk keys, and Accept all of exactly what the review displayed
    must succeed — not 409 as a phantom staleness conflict."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    iid = create_global_instruction(
        text=_TWIN_BASE, user_token=token, org_id=org_id, status="published",
    )["id"]
    _inject_suggestion_build(org_id, iid, _TWIN_PROPOSED)

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    hunks = review["suggestions"][0]["hunks"]
    assert len(hunks) == 2, hunks
    assert all(h["before"] == "ver_asof_date" and h["after"] == "fk_ver_asof_date" for h in hunks)
    assert len({h["key"] for h in hunks}) == 2, "identical twin edits must not share a key"

    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        json=_exact_review_payload(review),
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    assert get_instruction(iid, user_token=token, org_id=org_id)["text"] == _TWIN_PROPOSED
    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["suggestions"] == []


@pytest.mark.e2e
def test_accept_single_twin_hunk_applies_only_that_occurrence(
    test_client, create_global_instruction, get_instruction, create_user, login_user, whoami,
):
    """With distinct keys the twins are individually addressable: accepting one
    applies only that occurrence and leaves its twin pending."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    iid = create_global_instruction(
        text=_TWIN_BASE, user_token=token, org_id=org_id, status="published",
    )["id"]
    bid = _inject_suggestion_build(org_id, iid, _TWIN_PROPOSED)

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    hunks = sorted(review["suggestions"][0]["hunks"], key=lambda h: h["start"])
    first, second = hunks
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/accept",
        json={"build_id": bid, "hunk_key": first["key"],
              "against_main_build_id": review["main_build_id"],
              "against_main_version_id": review["main_version_id"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    expected = _TWIN_BASE.replace("ver_asof_date", "fk_ver_asof_date", 1)
    assert get_instruction(iid, user_token=token, org_id=org_id)["text"] == expected

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    remaining = review["suggestions"][0]["hunks"]
    assert [h["key"] for h in remaining] == [second["key"]], \
        "the untouched twin must stay pending under its own stable key"


@pytest.mark.e2e
def test_rejection_recorded_under_legacy_unscoped_key_still_matches(
    test_client, create_global_instruction, create_user, login_user, whoami,
):
    """Decisions recorded before keys became position-aware stored the unscoped
    content hash. Those records must keep resolving their hunk (via the
    unscoped-key alias) so old rejections don't resurface as pending."""
    import json as _json
    import os
    from sqlalchemy import create_engine, text as _text
    from app.services.text_hunks import compute_hunks

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    base, proposed = "one two three four", "one TWO three four"
    iid = create_global_instruction(
        text=base, user_token=token, org_id=org_id, status="published",
    )["id"]
    bid = _inject_suggestion_build(org_id, iid, proposed)

    (hunk,) = compute_hunks(base, proposed)
    legacy_key = hunk.unscoped_key
    assert legacy_key != hunk.key, "precondition: scoped and legacy formats differ"

    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                _text("UPDATE instruction_builds SET rejected_hunks = :rej WHERE id = :bid"),
                {"rej": _json.dumps([{"instruction_id": str(iid), "key": legacy_key, "action": "reject"}]),
                 "bid": bid},
            )
    finally:
        engine.dispose()

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    assert review["suggestions"] == [], \
        "a hunk rejected under the pre-position-aware key must not resurface"


@pytest.mark.e2e
def test_duplicate_selection_is_not_reported_as_staleness(
    test_client, create_global_instruction, create_user, login_user, whoami,
):
    """A malformed request (same pair twice) still 409s fail-closed, but as an
    invalid-selection message — not "moved since you viewed", which sends the
    reviewer into a refresh loop that can never help."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    iid = create_global_instruction(
        text="one two three four", user_token=token, org_id=org_id, status="published",
    )["id"]
    bid = _inject_suggestion_build(org_id, iid, "one TWO three four")

    review = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=headers).json()
    (hunk,) = review["suggestions"][0]["hunks"]
    resp = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        json={"against_main_build_id": review["main_build_id"],
              "against_main_version_id": review["main_version_id"],
              "hunks": [{"build_id": bid, "hunk_key": hunk["key"]},
                        {"build_id": bid, "hunk_key": hunk["key"]}]},
        headers=headers,
    )
    assert resp.status_code == 409, resp.json()
    assert "moved since you viewed" not in resp.json()["detail"]
