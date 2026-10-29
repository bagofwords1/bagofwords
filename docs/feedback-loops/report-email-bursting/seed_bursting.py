"""Seed the report-email bursting scenario into a running stack's DB.

Run from backend/ with the stack's env (same BOW_CONFIG_PATH / database), after
tools/agent/seed_org.py created admin@example.com (the OWNER) and invited
alice@, bob@ and carol@example.com as members. Writes a JSON of ids to argv[1].

  identity  page dashboard over an identity-scoped query ("my accounts":
            source=identity param, code filters a book of accounts by the
            running user's email). The stored snapshot is the OWNER's rows
            OWNER-ACCT-*; alice owns ALICE-ACCT-*, bob BOB-ACCT-*.
  plain     page dashboard over a plain query (PLAIN-ROW-*).
  deck      slides artifact over the same identity query; its .pptx on disk
            is what slide generation baked for the owner (OWNER-ACCT-*), and
            its content['code'] is the python-pptx script that produced it.

Steps/visualizations/artifacts come from the agent in production (no CRUD
API), so they are written directly, like the other feedback-loop seeds.
"""
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, ".")
import main  # noqa: F401,E402
from sqlalchemy import select  # noqa: E402

from app.dependencies import async_session_maker  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.query import Query  # noqa: E402
from app.models.report import Report  # noqa: E402
from app.models.step import Step  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.visualization import Visualization  # noqa: E402
from app.models.widget import Widget  # noqa: E402
from app.services.report_pdf_service import ReportPdfService  # noqa: E402
from tests.fixtures.artifact import seed_artifact  # noqa: E402

OWNER, ALICE, BOB, CAROL = ("admin@example.com", "alice@example.com",
                            "bob@example.com", "carol@example.com")
BOOK = {
    OWNER: ["OWNER-ACCT-1", "OWNER-ACCT-2", "OWNER-ACCT-3"],
    ALICE: ["ALICE-ACCT-1", "ALICE-ACCT-2"],
    BOB: ["BOB-ACCT-1"],
    CAROL: ["CAROL-ACCT-1"],
}
IDENTITY = [{"name": "Viewer", "type": "string", "label": "Viewer", "default": None,
             "required": False, "source": "identity", "identity_binding": "viewer.email",
             "options": None, "options_source": None, "strict_options": False}]

ID_CODE = f"""
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    book = {BOOK!r}
    rows = [(rep, a) for rep, accts in book.items() for a in accts]
    df = pd.DataFrame(rows, columns=["rep", "account"])
    return df[df["rep"] == params["Viewer"]].reset_index(drop=True)
"""
PLAIN_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"account": [f"PLAIN-ROW-{i}" for i in range(4)], "amount": [10, 20, 30, 40]})
"""

# Every row label lands in the PDF text layer.
PAGE_CODE = """<script type="text/babel">
function App() {
  const data = useArtifactData();
  if (!data) return <div>loading</div>;
  return (<div className="p-6"><h1 className="text-2xl font-bold">{data.report.title}</h1>
    {data.visualizations.map((v, k) => (<div key={k}><h2>{v.title}: {(v.rows||[]).length} rows</h2>
      <table><tbody>{(v.rows||[]).map((r,i)=><tr key={i}><td>{r.account}</td></tr>)}</tbody></table></div>))}
    <p>END-OF-DASHBOARD</p></div>);
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

DECK_CODE = """
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = report["title"] or "Accounts"
box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(4)).text_frame
box.text = "Accounts"
for r in visualizations[0]["rows"]:
    box.add_paragraph().text = str(r["account"])
prs.save(_pptx_output_path)
"""


async def query(db, org, owner, report, title, identity):
    s = uuid.uuid4().hex[:6]
    w = Widget(title=title, slug=f"w-{s}", report_id=report.id)
    db.add(w); await db.flush()
    if identity:
        data = {"rows": [{"rep": OWNER, "account": a} for a in BOOK[OWNER]],
                "columns": [{"field": "rep"}, {"field": "account"}]}
    else:
        data = {"rows": [{"account": f"PLAIN-ROW-{i}", "amount": 10 * (i + 1)} for i in range(4)],
                "columns": [{"field": "account"}, {"field": "amount"}]}
    st = Step(title=title, slug=f"s-{s}", status="success", widget_id=w.id,
              code=ID_CODE if identity else PLAIN_CODE, data=data, data_model={"type": "table"},
              applied_params={"Viewer": OWNER} if identity else None)
    db.add(st); await db.flush()
    q = Query(title=title, report_id=report.id, widget_id=w.id, default_step_id=st.id,
              parameters=IDENTITY if identity else [], organization_id=org.id, user_id=owner.id)
    db.add(q); await db.flush()
    st.query_id = q.id
    v = Visualization(title=title, status="success", report_id=report.id, query_id=q.id,
                      view={"type": "table"})
    db.add(v); await db.flush()
    return q, v, data


async def report(db, org, owner, title, identity, mode="page"):
    r = Report(title=title, slug=f"burst-{uuid.uuid4().hex[:6]}", organization_id=org.id,
               user_id=owner.id, status="published")
    db.add(r); await db.flush()
    q, v, data = await query(db, org, owner, r, f"{title} rows", identity)
    a = await seed_artifact(db, report_id=r.id, user_id=owner.id, organization_id=org.id,
                            mode=mode, title=title,
                            content={"code": DECK_CODE if mode == "slides" else PAGE_CODE,
                                     "visualization_ids": [str(v.id)]})
    if mode == "slides":
        # What slide generation does: run the stored code once, over the
        # owner's snapshot, and keep the file.
        from app.ai.code_execution.pptx_executor import PptxCodeExecutor

        ReportPdfService.PPTX_DIR.mkdir(parents=True, exist_ok=True)
        path = ReportPdfService.PPTX_DIR / f"{a.id}.pptx"
        PptxCodeExecutor().execute_pptx_code(
            code=DECK_CODE, report={"id": str(r.id), "title": title, "theme": None},
            visualizations=[{"id": str(v.id), "title": title, **data}], output_path=Path(path),
        )
        a.pptx_path = str(path)
    return {"report_id": str(r.id), "artifact_id": str(a.id), "query_id": str(q.id)}


async def main_():
    async with async_session_maker() as db:
        owner = (await db.execute(select(User).where(User.email == OWNER))).scalar_one()
        org = (await db.execute(select(Organization))).scalars().first()
        out = {
            "org_id": str(org.id),
            "identity": await report(db, org, owner, "Identity accounts", True),
            "plain": await report(db, org, owner, "Plain revenue", False),
            "deck": await report(db, org, owner, "Identity deck", True, mode="slides"),
        }
        await db.commit()
        with open(sys.argv[1], "w") as fh:
            json.dump(out, fh)


asyncio.run(main_())
