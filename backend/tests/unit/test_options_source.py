"""options_source — the filter-space pattern (one query feeds another's
control choices). Save-time validation contracts."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.schemas.param_schema import ParamSpec
from app.ai.code_execution.query_params import ParamError
from app.services.query_service import QueryService


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed(db):
    org = Organization(name="o")
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add_all([org, user])
    await db.flush()
    report = Report(title="r", user_id=str(user.id), organization_id=str(org.id), slug="r1", status="published")
    db.add(report)
    await db.flush()

    def mk_query(title, slug):
        w = Widget(title=title, slug=f"w-{slug}", report_id=str(report.id), status="draft")
        db.add(w)
        return w

    w_g = mk_query("Genres", "g")
    w_a = mk_query("Albums", "a")
    await db.flush()
    genres = Query(title="Genres", report_id=str(report.id), widget_id=str(w_g.id), organization_id=str(org.id))
    albums = Query(title="Albums", report_id=str(report.id), widget_id=str(w_a.id), organization_id=str(org.id))
    db.add_all([genres, albums])
    await db.flush()
    step = Step(
        title="Genres", slug="step-g", status="success", type="table",
        code="def generate_df(c,e):\n    return None",
        widget_id=str(w_g.id), query_id=str(genres.id),
        data={"columns": [{"field": "GenreId"}, {"field": "Name"}], "rows": []},
    )
    db.add(step)
    await db.flush()
    genres.default_step_id = str(step.id)
    db.add(genres)
    await db.flush()
    return report, genres, albums


def _spec(query_id, value_column="Name", label_column=None):
    return ParamSpec.model_validate({
        "name": "genre", "type": "string", "source": "input",
        "options_source": {
            "query_id": str(query_id), "value_column": value_column,
            "label_column": label_column,
        },
    })


@pytest.mark.asyncio
async def test_valid_options_source_passes(db):
    report, genres, albums = await _seed(db)
    await QueryService()._validate_options_sources(db, albums, [_spec(genres.id, "Name", "GenreId")])


@pytest.mark.asyncio
async def test_self_reference_rejected(db):
    report, genres, albums = await _seed(db)
    with pytest.raises(ParamError, match="different"):
        await QueryService()._validate_options_sources(db, albums, [_spec(albums.id)])


@pytest.mark.asyncio
async def test_unknown_query_rejected(db):
    report, genres, albums = await _seed(db)
    with pytest.raises(ParamError, match="not found"):
        await QueryService()._validate_options_sources(db, albums, [_spec("00000000-0000-0000-0000-000000000000")])


@pytest.mark.asyncio
async def test_unknown_column_rejected(db):
    report, genres, albums = await _seed(db)
    with pytest.raises(ParamError, match="column 'Nope'"):
        await QueryService()._validate_options_sources(db, albums, [_spec(genres.id, "Nope")])


@pytest.mark.asyncio
async def test_resolve_refs_by_title_id_and_drop(db):
    """Agent refs canonicalize: exact title and id resolve to the query id;
    unresolvable and self-referencing refs drop the options_source."""
    from app.services.query_service import resolve_options_source_refs
    report, genres, albums = await _seed(db)

    def param(ref):
        return {"name": "genre", "type": "string", "source": "input",
                "options_source": {"query_id": ref, "value_column": "Name", "label_column": None}}

    out = await resolve_options_source_refs(
        db, str(report.id),
        [param("genres"), param(str(genres.id)), param("No Such Query"), param(str(albums.id))],
        exclude_query_id=str(albums.id),
    )
    assert out[0]["options_source"]["query_id"] == str(genres.id)   # title (case-insensitive)
    assert out[1]["options_source"]["query_id"] == str(genres.id)   # id passthrough
    assert out[2]["options_source"] is None                          # unresolvable → dropped
    assert out[3]["options_source"] is None                          # self-ref → dropped


@pytest.mark.asyncio
async def test_no_step_data_yet_is_lenient(db):
    """A source query that has never run has no columns to check against —
    the reference itself is enough (columns validate on the next save)."""
    report, genres, albums = await _seed(db)
    genres.default_step_id = None
    db.add(genres)
    await db.flush()
    await QueryService()._validate_options_sources(db, albums, [_spec(genres.id, "Anything")])
