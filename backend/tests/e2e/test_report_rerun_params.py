"""
Regression guard: a report rerun must execute saved step code WITH the
parameter values the step last ran with.

Reported symptom: a customer built a dashboard with a filter, picked a value,
and everything rendered. The next morning a manual refresh reported that only
some objects had refreshed.

Root cause: StepService._execute_step_code called
StreamingCodeExecutor.execute_code_async without `params`. The executor then
injects `dict(params or {})` -- an EMPTY dict, which is not "run with
defaults": generated code does `params["X"]` and raises KeyError. The
interactive path (query_service) always passed `params=resolved_params`, so a
query worked when the filter was used and failed on every refresh afterwards.
The split was deterministic: queries WITHOUT parameters always refreshed,
queries WITH parameters never did.

Contract asserted here:
  1. rerun re-executes with the values stored on the step (applied_params),
     re-resolved through the same resolver the interactive path uses;
  2. identity-bound params are recomputed for whoever runs the refresh and are
     never replayed from what was stored;
  3. the per-viewer result cache is keyed by parameter combination, so a
     viewer holding several combinations does not break their refresh;
  4. the rerun records the values it actually executed with, so applied_params
     never drifts from the data beside it (it also seeds the next rerun);
  5. 'creator' share mode swaps CREDENTIALS only — identity params still bind
     the viewer, matching run_query_viewer.

Run:
    cd backend
    uv run pytest tests/e2e/test_report_rerun_params.py -v
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.artifact import Artifact
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.step_user_result import StepUserResult
from app.models.visualization import Visualization
from app.models.widget import Widget


def _run(coro):
    return asyncio.run(coro)


# Exactly the shape the agent generates today: three-arg signature, direct
# subscript of params. 9 of 9 such steps in a real deployment used [] and none
# used .get(), so the KeyError is the realistic failure, not an edge case.
PARAM_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({"ran_for": [params["BillingYear"]], "revenue": [42]})
"""

NO_PARAM_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"ran_for": ["n/a"], "revenue": [7]})
"""

IDENTITY_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({"ran_for": [params["Viewer"]], "revenue": [1]})
"""


def _param_spec(name="BillingYear", source="input", identity_binding=None,
                default=None):
    return {
        "name": name,
        "type": "string",
        "label": name,
        "default": default,
        "required": False,
        "source": source,
        "identity_binding": identity_binding,
        "options": None,
        "options_source": None,
        "strict_options": False,
    }


async def _seed(report_id, specs_and_code, applied_params_per_query):
    """Seed an artifact dashboard whose queries declare parameters.

    Queries/steps/visualizations/artifacts are produced by the AI flow in
    production and have no public CRUD API, so the graph is seeded directly
    (same approach as test_report_rerun_artifact.py).
    """
    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org_id, user_id = report.organization_id, report.user_id

        query_ids, viz_ids, step_ids = [], [], []
        for qi, (specs, code) in enumerate(specs_and_code):
            widget = Widget(title=f"W{qi} {suffix}", slug=f"w{qi}-{suffix}", report_id=report_id)
            db.add(widget)
            await db.flush()

            query = Query(
                title=f"Query {qi}",
                report_id=report_id,
                widget_id=widget.id,
                organization_id=org_id,
                user_id=user_id,
                parameters=specs,
            )
            db.add(query)
            await db.flush()

            step = Step(
                title=f"Step {qi}",
                slug=f"s{qi}-{suffix}",
                status="success",
                widget_id=widget.id,
                query_id=query.id,
                code=code,
                data={"rows": [{"ran_for": "STALE", "revenue": -1}],
                      "columns": [{"field": "ran_for"}, {"field": "revenue"}]},
                applied_params=applied_params_per_query[qi],
                created_at=now - timedelta(hours=2),
            )
            db.add(step)
            await db.flush()
            query.default_step_id = step.id

            viz = Visualization(title=f"Viz {qi}", status="success",
                                report_id=report_id, query_id=query.id,
                                view={"type": "table"})
            db.add(viz)
            await db.flush()

            query_ids.append(str(query.id))
            viz_ids.append(str(viz.id))
            step_ids.append(str(step.id))

        db.add(Artifact(
            report_id=report_id, user_id=user_id, organization_id=org_id,
            title="Dashboard", mode="page", version=1, status="completed",
            content={"code": "function App() {}", "visualization_ids": viz_ids},
        ))
        await db.commit()

    return {"query_ids": query_ids, "step_ids": step_ids, "viz_ids": viz_ids}


async def _step_rows(step_id):
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        return (step.data or {}).get("rows") or []


async def _step_applied_params(step_id):
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        return step.applied_params


@pytest.mark.e2e
def test_rerun_executes_parameterized_steps_with_their_stored_values(
    create_report, create_user, login_user, whoami, rerun_report
):
    """A query that declares a parameter must refresh, using the value the
    step last ran with — not an empty dict, and not the declared default."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Filter dashboard", user_token=token,
                           org_id=org_id, data_sources=[])
    seeded = _run(_seed(
        report["id"],
        specs_and_code=[
            ([_param_spec()], PARAM_CODE),   # with a filter
            ([], NO_PARAM_CODE),             # without one
        ],
        applied_params_per_query=[{"BillingYear": "2023"}, None],
    ))

    body = rerun_report(report["id"], user_token=token, org_id=org_id)

    # Before the fix this was 1 succeeded / 1 failed: the parameterized query
    # raised KeyError('BillingYear') while the plain one refreshed.
    assert body["steps_total"] == 2
    assert body["steps_failed"] == 0, body
    assert body["steps_succeeded"] == 2

    param_rows = _run(_step_rows(seeded["step_ids"][0]))
    assert param_rows and param_rows[0]["ran_for"] == "2023", param_rows
    assert param_rows[0]["revenue"] == 42

    plain_rows = _run(_step_rows(seeded["step_ids"][1]))
    assert plain_rows and plain_rows[0]["revenue"] == 7


@pytest.mark.e2e
def test_rerun_recomputes_identity_params_instead_of_replaying_them(
    create_report, create_user, login_user, whoami, rerun_report
):
    """An identity-bound param must resolve to the user running the refresh.

    Replaying applied_params verbatim would both leak another user's identity
    into the run and raise ParamError, since resolve_param_values rejects a
    submitted value for an identity param.
    """
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Identity dashboard", user_token=token,
                           org_id=org_id, data_sources=[])
    seeded = _run(_seed(
        report["id"],
        specs_and_code=[([
            _param_spec(name="Viewer", source="identity",
                        identity_binding="viewer.email"),
        ], IDENTITY_CODE)],
        # A stale value belonging to somebody else. It must be ignored.
        applied_params_per_query=[{"Viewer": "someone-else@example.com"}],
    ))

    body = rerun_report(report["id"], user_token=token, org_id=org_id)
    assert body["steps_failed"] == 0, body

    rows = _run(_step_rows(seeded["step_ids"][0]))
    assert rows, "identity-bound step was not refreshed"
    assert rows[0]["ran_for"] == user["email"]
    assert rows[0]["ran_for"] != "someone-else@example.com"

    # The re-derived value is also what the step now records as "what this run
    # executed with" — leaving the stale dict there would both misreport the
    # run and feed the next rerun's resolution.
    assert _run(_step_applied_params(seeded["step_ids"][0])) == {
        "Viewer": user["email"]
    }


@pytest.mark.e2e
def test_rerun_records_the_values_it_actually_executed_with(
    create_report, create_user, login_user, whoami, rerun_report
):
    """applied_params must reflect the resolved values, not the stored ones.

    Resolution fills in declared defaults for params the last run never had a
    value for. If the rerun does not write them back, applied_params keeps
    claiming the step ran with nothing while the data says otherwise — and
    that stale dict is what seeds the NEXT rerun.
    """
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Defaulted filter", user_token=token,
                           org_id=org_id, data_sources=[])
    seeded = _run(_seed(
        report["id"],
        specs_and_code=[([_param_spec(default="2020")], PARAM_CODE)],
        # Never ran with an explicit value — the default is the only source.
        applied_params_per_query=[None],
    ))

    body = rerun_report(report["id"], user_token=token, org_id=org_id)
    assert body["steps_failed"] == 0, body

    rows = _run(_step_rows(seeded["step_ids"][0]))
    assert rows and rows[0]["ran_for"] == "2020", rows
    assert _run(_step_applied_params(seeded["step_ids"][0])) == {
        "BillingYear": "2020"
    }


async def _creator_mode_run(step_id, report_id, viewer_id, owner_id):
    """Run a step in 'creator' share mode: the owner's credentials, the
    viewer's identity."""
    from app.models.user import User
    from app.services.step_service import StepService

    async with async_session_maker() as db:
        viewer = await db.get(User, viewer_id)
        owner = await db.get(User, owner_id)
        return await StepService().run_step_to_user_result(
            db, str(step_id), run_user=viewer, credential_user=owner,
            executed_as="creator",
        )


@pytest.mark.e2e
def test_creator_mode_binds_identity_params_to_the_viewer(
    create_report, create_user, login_user, whoami, test_client
):
    """'creator' mode swaps CREDENTIALS, not identity.

    query_service.run_query_viewer resolves identity params from the caller in
    both share modes (the personalization tier: the owner's connection runs
    the query, the caller's identity scopes it). Binding them to the
    credential user here instead would make the same dashboard return
    different rows depending on whether the viewer moved a filter or hit
    refresh.
    """
    owner = create_user()
    owner_token = login_user(owner["email"], owner["password"])
    org_id = whoami(owner_token)["organizations"][0]["id"]

    # Second org member: invite, then register against that invite (sign-up is
    # closed after the first user, so create_user picks the token up itself).
    viewer_email = f"viewer_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": viewer_email, "role": "member"},
        headers={"Authorization": f"Bearer {owner_token}",
                 "X-Organization-Id": str(org_id)},
    )
    viewer = create_user(email=viewer_email, password="test123")

    report = create_report(title="Creator-mode dashboard", user_token=owner_token,
                           org_id=org_id, data_sources=[])
    seeded = _run(_seed(
        report["id"],
        specs_and_code=[([
            _param_spec(name="Viewer", source="identity",
                        identity_binding="viewer.email"),
        ], IDENTITY_CODE)],
        applied_params_per_query=[None],
    ))

    async def _ids():
        async with async_session_maker() as db:
            from app.models.user import User as _U
            r = await db.get(Report, report["id"])
            v = (await db.execute(
                select(_U).where(_U.email == viewer["email"])
            )).scalars().first()
            return str(r.user_id), str(v.id)

    owner_id, viewer_id = _run(_ids())

    row = _run(_creator_mode_run(seeded["step_ids"][0], report["id"],
                                 viewer_id, owner_id))

    assert row.status == "success", row.status_reason
    assert row.executed_as == "creator"
    assert (row.data or {}).get("rows")[0]["ran_for"] == viewer["email"]
    assert (row.data or {}).get("rows")[0]["ran_for"] != owner["email"]
    assert row.applied_params == {"Viewer": viewer["email"]}


async def _viewer_run(step_id, report_id, user_id, fingerprints):
    """Give a viewer several cached parameter combinations, then refresh."""
    from app.models.user import User
    from app.services.step_service import StepService

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = await db.get(User, user_id)
        # Space the rows minutes apart rather than letting successive
        # utcnow() calls decide the order: which one is "most recent" is the
        # whole point of the assertion, and on a backend that stores DATETIME
        # at second precision two microsecond-apart rows tie and the ordering
        # goes non-deterministic.
        base = datetime.utcnow() - timedelta(hours=1)
        for i, (fp, value) in enumerate(fingerprints):
            db.add(StepUserResult(
                step_id=str(step_id), user_id=str(user_id),
                organization_id=str(report.organization_id),
                report_id=str(report_id),
                status="success", data={"rows": [], "columns": []},
                executed_as="viewer",
                params_fingerprint=fp,
                applied_params={"BillingYear": value},
                last_run_at=base + timedelta(minutes=i),
            ))
        await db.commit()

        return await StepService().run_step_to_user_result(
            db, str(step_id), run_user=user, credential_user=user,
            executed_as="viewer",
        )


@pytest.mark.e2e
def test_viewer_rerun_survives_multiple_cached_parameter_combinations(
    create_report, create_user, login_user, whoami
):
    """The per-viewer cache is keyed (step_id, user_id, params_fingerprint).

    A lookup that ignores the fingerprint and calls scalar_one_or_none()
    raises MultipleResultsFound the moment a viewer has picked two different
    filter values — which is the normal way a filter gets used.
    """
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Viewer dashboard", user_token=token,
                           org_id=org_id, data_sources=[])
    seeded = _run(_seed(
        report["id"],
        specs_and_code=[([_param_spec()], PARAM_CODE)],
        applied_params_per_query=[{"BillingYear": "2022"}],
    ))
    step_id = seeded["step_ids"][0]

    async def _user_id():
        async with async_session_maker() as db:
            r = await db.get(Report, report["id"])
            return str(r.user_id)

    uid = _run(_user_id())

    row = _run(_viewer_run(step_id, report["id"], uid,
                           [("fp-2022", "2022"), ("fp-2024", "2024")]))

    assert row.status == "success", row.status_reason
    # Most recent cached combination wins, and the result lands in that
    # combination's own slot rather than overwriting the no-params one.
    assert row.applied_params == {"BillingYear": "2024"}
    assert row.params_fingerprint not in ("", "fp-2022")
    assert (row.data or {}).get("rows")[0]["ran_for"] == "2024"


# --- identity-bind verification -------------------------------------------
# Saved code can lose its identity predicate (a regenerated step, a hand-edited
# query). run_query_viewer already refuses to serve the result; the rerun paths
# must too, now that they can execute identity params at all.

IDENTITY_SCOPED_SQL = """
def generate_df(ds_clients, excel_files, params):
    client = list(ds_clients.values())[0]
    return client.execute_query(
        "SELECT owner, revenue FROM billing WHERE owner = :Viewer",
        params={"Viewer": params["Viewer"]},
    )
"""

IDENTITY_UNSCOPED_SQL = """
def generate_df(ds_clients, excel_files, params):
    client = list(ds_clients.values())[0]
    return client.execute_query("SELECT owner, revenue FROM billing")
"""


class _FakeClient:
    """Enough surface for wrap_clients_for_capture to wrap it (it keys off
    `execute_query`), so the real capture + :name rendering path runs."""

    def execute_query(self, query, **kwargs):
        import pandas as pd
        return pd.DataFrame({"owner": ["someone@example.com"], "revenue": [5]})


async def _viewer_run_with_client(step_id, user_id):
    from app.models.user import User
    from app.services.step_service import StepService

    async with async_session_maker() as db:
        user = await db.get(User, user_id)
        return await StepService().run_step_to_user_result(
            db, str(step_id), run_user=user, credential_user=user,
            executed_as="viewer", db_clients={"c": _FakeClient()},
        )


async def _owner_rerun_with_client(step_id, user_id):
    from app.models.user import User
    from app.services.step_service import StepService

    async with async_session_maker() as db:
        user = await db.get(User, user_id)
        return await StepService().rerun_step(
            db, str(step_id), current_user=user,
            db_clients={"c": _FakeClient()},
        )


def _seed_identity_sql_report(create_report, token, org_id, code):
    report = create_report(title="Identity SQL", user_token=token,
                           org_id=org_id, data_sources=[])
    seeded = _run(_seed(
        report["id"],
        specs_and_code=[([
            _param_spec(name="Viewer", source="identity",
                        identity_binding="viewer.email"),
        ], code)],
        applied_params_per_query=[None],
    ))

    async def _uid():
        async with async_session_maker() as db:
            r = await db.get(Report, report["id"])
            return str(r.user_id)

    return report, seeded["step_ids"][0], _run(_uid())


@pytest.mark.e2e
def test_viewer_rerun_refuses_results_when_the_identity_bind_is_missing(
    create_report, create_user, login_user, whoami
):
    """Code that dropped its identity predicate must not produce rows."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    _report, step_id, uid = _seed_identity_sql_report(
        create_report, token, org_id, IDENTITY_UNSCOPED_SQL)

    row = _run(_viewer_run_with_client(step_id, uid))

    assert row.status == "error", row.data
    assert "identity parameter" in (row.status_reason or "")
    assert not (row.data or {}).get("rows")


@pytest.mark.e2e
def test_viewer_rerun_serves_results_when_the_identity_bind_is_applied(
    create_report, create_user, login_user, whoami
):
    """The same check must not fire on correctly scoped code."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    _report, step_id, uid = _seed_identity_sql_report(
        create_report, token, org_id, IDENTITY_SCOPED_SQL)

    row = _run(_viewer_run_with_client(step_id, uid))

    assert row.status == "success", row.status_reason
    assert (row.data or {}).get("rows")


@pytest.mark.e2e
def test_owner_rerun_refuses_to_write_an_unscoped_shared_snapshot(
    create_report, create_user, login_user, whoami
):
    """The shared snapshot is withheld from other viewers when the step is
    identity-scoped, but the owner's own rerun must still not persist rows the
    identity predicate never filtered."""
    from app.ai.code_execution.query_params import ParamError

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    _report, step_id, uid = _seed_identity_sql_report(
        create_report, token, org_id, IDENTITY_UNSCOPED_SQL)

    with pytest.raises(ParamError, match="identity parameter"):
        _run(_owner_rerun_with_client(step_id, uid))

    # The stale snapshot stays; it is never replaced with unscoped rows.
    rows = _run(_step_rows(step_id))
    assert rows and rows[0]["ran_for"] == "STALE"


# --- applied_params on the public /r route ---------------------------------
# The shared-report step endpoint serves anonymous readers. `code` and `data`
# are withheld for an identity-scoped step; applied_params must not be the one
# field that ships the snapshot producer's identity (an email, a department)
# to every reader.

async def _publish(report_id):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        report.artifact_visibility = "public"
        report.status = "published"
        await db.commit()


@pytest.mark.e2e
def test_public_step_withholds_an_input_identity_default_snapshot(
    create_report, create_user, login_user, whoami, test_client
):
    """input_identity_default resolves from the identity binding whenever the
    client submits nothing — which is exactly how a snapshot is materialized
    (owner refresh, schedule, refresh-on-view all submit nothing). So the
    shared snapshot is filtered by the OWNER's identity and must be withheld
    from other readers, same as source='identity'.

    Both the rows and the stored value have to go: the value is an identity,
    and the rows are that identity's slice.
    """
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Public dashboard", user_token=token,
                           org_id=org_id, data_sources=[])
    seeded = _run(_seed(
        report["id"],
        specs_and_code=[([
            _param_spec(name="Dept", source="input_identity_default",
                        identity_binding="viewer.profile_attributes.department"),
            _param_spec(name="BillingYear"),
        ], NO_PARAM_CODE)],
        applied_params_per_query=[{
            "Dept": "secret-department",
            "BillingYear": "2023",
        }],
    ))
    _run(_publish(report["id"]))

    # Anonymous: no Authorization header at all.
    resp = test_client.get(
        f"/api/r/{report['id']}/queries/{seeded['query_ids'][0]}/step")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["snapshot_withheld"] is True
    assert body["code"] == ""
    assert not (body.get("data") or {}).get("rows")
    assert "secret-department" not in resp.text
    assert body["applied_params"] is None


@pytest.mark.e2e
def test_public_step_withholds_applied_params_with_the_withheld_snapshot(
    create_report, create_user, login_user, whoami, test_client
):
    """A source='identity' param taints the step, so the snapshot is withheld.
    applied_params must go with it — it holds the identity the snapshot was
    materialized under."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    report = create_report(title="Identity dashboard", user_token=token,
                           org_id=org_id, data_sources=[])
    seeded = _run(_seed(
        report["id"],
        specs_and_code=[([
            _param_spec(name="Viewer", source="identity",
                        identity_binding="viewer.email"),
        ], IDENTITY_CODE)],
        applied_params_per_query=[{"Viewer": "owner-private@example.com"}],
    ))
    _run(_publish(report["id"]))

    resp = test_client.get(
        f"/api/r/{report['id']}/queries/{seeded['query_ids'][0]}/step")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["snapshot_withheld"] is True
    assert body["code"] == ""
    assert not (body.get("data") or {}).get("rows")
    assert "owner-private@example.com" not in resp.text
    assert body["applied_params"] is None
