"""Ids named in instruction, agent, LLM-provider and eval requests must belong
to the caller's organization.

Each test pairs the refusal (an id of another organization reads as missing
and none of its content comes back) with the same request using the caller's
own id, so a fix that simply breaks the route cannot pass.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.datasource_table import DataSourceTable
from app.models.instruction_reference import InstructionReference
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider
from app.models.metadata_resource import MetadataResource

from ._cast import chinook_agent, h, two_orgs  # noqa: F401  (fixtures)


async def _a_table_of(data_source_id):
    async with async_session_maker() as db:
        row = (await db.execute(
            select(DataSourceTable.id, DataSourceTable.name)
            .where(DataSourceTable.datasource_id == data_source_id)
            .order_by(DataSourceTable.name).limit(1)
        )).first()
        assert row is not None, "the data source has no indexed tables"
        return str(row[0]), row[1]


async def _seed_metadata_resource(org_id, data_source_id, name):
    # Metadata resources are only written by a git/dbt indexing job; there is
    # no API to create one, so the row is seeded directly.
    async with async_session_maker() as db:
        res = MetadataResource(
            name=name, resource_type="dbt_model", organization_id=org_id,
            data_source_id=data_source_id, description=f"description of {name}",
            sql_content=f"select 1 as {name}",
        )
        db.add(res)
        await db.commit()
        return str(res.id)


async def _seed_reference(instruction_id, object_type, object_id):
    # A reference row that the API now refuses to write: seeded directly to
    # prove rows stored before the check do not hydrate either.
    async with async_session_maker() as db:
        db.add(InstructionReference(
            instruction_id=instruction_id, object_type=object_type,
            object_id=object_id, display_text="stored earlier",
        ))
        await db.commit()


def _create_global(test_client, token, org, references, data_source_ids=None):
    return test_client.post(
        "/api/instructions/global",
        json={"text": f"rule {uuid.uuid4().hex[:6]}", "status": "published",
              "category": "general", "data_source_ids": data_source_ids or [],
              "references": references},
        headers=h(token, org),
    )


@pytest.mark.e2e
@pytest.mark.parametrize("object_type", ["datasource_table", "metadata_resource"])
def test_instruction_reference_must_point_into_the_callers_org(
    test_client, two_orgs, chinook_agent, object_type,
):
    c = two_orgs
    own_ds = chinook_agent(user_token=c["a_token"], org_id=c["a_org"])
    other_ds = chinook_agent(user_token=c["b_token"], org_id=c["b_org"])

    def _object_in(org, ds, marker):
        if object_type == "datasource_table":
            return asyncio.run(_a_table_of(ds["id"]))[0]
        return asyncio.run(_seed_metadata_resource(org, ds["id"], marker))

    other_marker = f"other_{uuid.uuid4().hex[:8]}"
    other_id = _object_in(c["b_org"], other_ds, other_marker)
    own_id = _object_in(c["a_org"], own_ds, f"own_{uuid.uuid4().hex[:8]}")

    # Another organization's object: refused on create, with or without a
    # data-source selection on the instruction.
    for ds_ids in ([], [own_ds["id"]]):
        refused = _create_global(
            test_client, c["a_token"], c["a_org"],
            [{"object_type": object_type, "object_id": other_id}], ds_ids)
        assert 400 <= refused.status_code < 500, refused.text
        assert other_marker not in refused.text

    # ... and on update of an instruction the caller owns.
    ok = _create_global(test_client, c["a_token"], c["a_org"],
                        [{"object_type": object_type, "object_id": own_id}])
    assert ok.status_code == 200, ok.text
    created = ok.json()
    refs = created["references"]
    assert [r["object_id"] for r in refs] == [own_id]
    assert refs[0]["object"]["id"] == own_id  # the caller's own object still hydrates

    upd = test_client.put(
        f"/api/instructions/{created['id']}",
        json={"references": [{"object_type": object_type, "object_id": other_id}]},
        headers=h(c["a_token"], c["a_org"]))
    assert 400 <= upd.status_code < 500, upd.text
    got = test_client.get(f"/api/instructions/{created['id']}", headers=h(c["a_token"], c["a_org"]))
    assert got.status_code == 200, got.text
    assert other_id not in [r["object_id"] for r in got.json()["references"]]


@pytest.mark.e2e
@pytest.mark.parametrize("object_type", ["datasource_table", "metadata_resource"])
def test_a_stored_reference_to_another_org_does_not_hydrate(
    test_client, two_orgs, chinook_agent, object_type,
):
    c = two_orgs
    other_ds = chinook_agent(user_token=c["b_token"], org_id=c["b_org"],
                             name=f"other_agent_{uuid.uuid4().hex[:8]}")
    if object_type == "datasource_table":
        other_id, other_name = asyncio.run(_a_table_of(other_ds["id"]))
    else:
        other_name = f"other_{uuid.uuid4().hex[:8]}"
        other_id = asyncio.run(_seed_metadata_resource(c["b_org"], other_ds["id"], other_name))

    ok = _create_global(test_client, c["a_token"], c["a_org"], [])
    assert ok.status_code == 200, ok.text
    asyncio.run(_seed_reference(ok.json()["id"], object_type, other_id))

    got = test_client.get(f"/api/instructions/{ok.json()['id']}", headers=h(c["a_token"], c["a_org"]))
    assert got.status_code == 200, got.text
    ref = next(r for r in got.json()["references"] if r["object_id"] == other_id)
    assert not ref.get("object")
    assert not ref.get("data_source_name")
    assert other_ds["name"] not in got.text
    if object_type == "metadata_resource":
        assert other_name not in got.text


@pytest.mark.e2e
def test_primary_instruction_must_be_one_of_the_orgs_own(test_client, two_orgs, chinook_agent, create_global_instruction):
    c = two_orgs
    own_ds = chinook_agent(user_token=c["a_token"], org_id=c["a_org"])
    secret = f"other org rule {uuid.uuid4().hex}"
    other = create_global_instruction(text=secret, user_token=c["b_token"], org_id=c["b_org"])
    own = create_global_instruction(text="own rule", user_token=c["a_token"], org_id=c["a_org"])
    url = f"/api/data_sources/{own_ds['id']}"

    refused = test_client.put(url, json={"primary_instruction_id": other["id"]}, headers=h(c["a_token"], c["a_org"]))
    assert refused.status_code == 404, refused.text
    got = test_client.get(url, headers=h(c["a_token"], c["a_org"]))
    assert got.status_code == 200
    assert got.json().get("primary_instruction_id") != other["id"]
    assert secret not in got.text

    ok = test_client.put(url, json={"primary_instruction_id": own["id"]}, headers=h(c["a_token"], c["a_org"]))
    assert ok.status_code == 200, ok.text
    got = test_client.get(url, headers=h(c["a_token"], c["a_org"])).json()
    assert got["primary_instruction_id"] == own["id"]
    assert got["primary_instruction"]["text"] == "own rule"
    # None still clears it.
    cleared = test_client.put(url, json={"primary_instruction_id": None}, headers=h(c["a_token"], c["a_org"]))
    assert cleared.status_code == 200, cleared.text
    assert test_client.get(url, headers=h(c["a_token"], c["a_org"])).json()["primary_instruction_id"] is None


async def _seed_provider(org_id, n_models=1, default=False):
    # Creating a provider through the API needs live provider credentials;
    # the rows are seeded directly (same as tests/e2e/rbac's scoping tests).
    async with async_session_maker() as db:
        provider = LLMProvider(organization_id=org_id, name=f"p_{uuid.uuid4().hex[:6]}",
                               provider_type="anthropic", is_preset=False, is_enabled=True,
                               use_preset_credentials=False)
        db.add(provider)
        await db.flush()
        ids = []
        for _ in range(n_models):
            model = LLMModel(organization_id=org_id, provider_id=provider.id, name="m",
                             model_id=f"claude-test-{uuid.uuid4().hex[:6]}", is_preset=False,
                             is_custom=True, is_enabled=True, is_default=default,
                             is_small_default=default)
            db.add(model)
            await db.flush()
            ids.append(str(model.id))
        await db.commit()
        return str(provider.id), ids


async def _model_enabled(model_id):
    async with async_session_maker() as db:
        return (await db.execute(select(LLMModel.is_enabled).where(LLMModel.id == model_id))).scalar_one()


@pytest.mark.e2e
def test_provider_update_only_edits_that_providers_models(test_client, two_orgs):
    c = two_orgs
    own_provider, (own_model,) = asyncio.run(_seed_provider(c["a_org"]))
    _, (other_model,) = asyncio.run(_seed_provider(c["b_org"]))
    url = f"/api/llm/providers/{own_provider}"

    refused = test_client.put(url, json={"models": [{"id": other_model, "model_id": "x", "is_enabled": False}]},
                              headers=h(c["a_token"], c["a_org"]))
    assert refused.status_code == 404, refused.text
    assert asyncio.run(_model_enabled(other_model)) is True

    ok = test_client.put(url, json={"models": [{"id": own_model, "model_id": "x", "is_enabled": False}]},
                         headers=h(c["a_token"], c["a_org"]))
    assert ok.status_code == 200, ok.text
    assert asyncio.run(_model_enabled(own_model)) is False


def _main_build_id(test_client, token, org):
    r = test_client.get("/api/builds/main", headers=h(token, org))
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.e2e
def test_eval_run_only_accepts_a_build_of_the_callers_org(
    test_client, two_orgs, create_global_instruction, create_test_suite, create_test_case,
):
    c = two_orgs
    create_global_instruction(text="other org rule", status="published", user_token=c["b_token"], org_id=c["b_org"])
    create_global_instruction(text="own rule", status="published", user_token=c["a_token"], org_id=c["a_org"])
    other_build = _main_build_id(test_client, c["b_token"], c["b_org"])
    own_build = _main_build_id(test_client, c["a_token"], c["a_org"])
    assert other_build != own_build

    asyncio.run(_seed_provider(c["a_org"], default=True))  # a run needs a default model
    suite = create_test_suite(name=f"s_{uuid.uuid4().hex[:6]}", user_token=c["a_token"], org_id=c["a_org"])
    case = create_test_case(suite_id=suite["id"], user_token=c["a_token"], org_id=c["a_org"])
    runs_before = test_client.get("/api/tests/runs", headers=h(c["a_token"], c["a_org"])).json()

    refused = test_client.post("/api/tests/runs", json={"case_ids": [case["id"]], "build_id": other_build},
                               headers=h(c["a_token"], c["a_org"]))
    assert refused.status_code == 404, refused.text
    assert test_client.get("/api/tests/runs", headers=h(c["a_token"], c["a_org"])).json() == runs_before

    ok = test_client.post("/api/tests/runs", json={"case_ids": [case["id"]], "build_id": own_build},
                          headers=h(c["a_token"], c["a_org"]))
    assert ok.status_code == 200, ok.text
    assert ok.json()["build_id"] == own_build


@pytest.mark.e2e
def test_org_level_metadata_resource_is_placed_by_its_own_organization(test_client, two_orgs):
    """A resource indexed from an organization-level repository has no data
    source: it is still referenceable inside its organization, and only there."""
    c = two_orgs
    own_id = asyncio.run(_seed_metadata_resource(c["a_org"], None, f"own_{uuid.uuid4().hex[:8]}"))
    other_name = f"other_{uuid.uuid4().hex[:8]}"
    other_id = asyncio.run(_seed_metadata_resource(c["b_org"], None, other_name))

    ok = _create_global(test_client, c["a_token"], c["a_org"],
                        [{"object_type": "metadata_resource", "object_id": own_id}])
    assert ok.status_code == 200, ok.text
    assert ok.json()["references"][0]["object"]["id"] == own_id

    refused = _create_global(test_client, c["a_token"], c["a_org"],
                             [{"object_type": "metadata_resource", "object_id": other_id}])
    assert 400 <= refused.status_code < 500, refused.text
    assert other_name not in refused.text
