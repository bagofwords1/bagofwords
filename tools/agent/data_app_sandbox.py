#!/usr/bin/env python3
"""Reproducible connected-data-app fixtures. Credentials are read from an external session file.

Run from backend with its configured sandbox environment:
  python ../tools/agent/data_app_sandbox.py seed --session /tmp/session.json --output /tmp/data-apps
  python ../tools/agent/data_app_sandbox.py generate --session /tmp/session.json --output /tmp/data-apps
Reports, queries and query execution use HTTP. Only Visualization rows use ORM:
there is no standalone visualization creation endpoint (normally the AI creates them).
"""

import argparse, asyncio, json, sqlite3, random, sys, time
from pathlib import Path
from datetime import date, timedelta
import httpx

P = argparse.ArgumentParser()
P.add_argument("action", choices=["seed", "generate", "install-fixtures"])
P.add_argument("--session", required=True)
P.add_argument("--output", required=True)
P.add_argument("--app", default="all")
args = P.parse_args()
out = Path(args.output)
out.mkdir(parents=True, exist_ok=True)
session = json.loads(Path(args.session).read_text())
headers = {
    "Authorization": "Bearer " + session["admin"]["token"],
    "X-Organization-Id": session["organization"]["id"],
}
c = httpx.Client(base_url=session["base_url"], headers=headers, timeout=180)


def api(method, path, **kw):
    r = c.request(method, path, **kw)
    if r.is_error:
        raise RuntimeError(f"{method} {path}: {r.status_code}: {r.text[:500]}")
    return r.json()


REGIONS = ["North America", "Europe", "Asia Pacific", "Latin America"]
CHANNELS = ["Direct", "Partner", "Marketplace"]
PARAMS = [
    {
        "name": "region",
        "type": "string",
        "label": "Region",
        "options": REGIONS,
        "strict_options": True,
    },
    {
        "name": "channels",
        "type": "list",
        "label": "Channels",
        "options": CHANNELS,
        "default": CHANNELS,
    },
    {
        "name": "period",
        "type": "date_range",
        "label": "Period",
        "default": {"from": "2026-01-01", "to": "2026-06-30"},
    },
    {
        "name": "min_order",
        "type": "number",
        "label": "Minimum order value",
        "default": 0,
    },
]
FILTER = """WHERE (:region IS NULL OR c.region=:region)
 AND o.channel IN :channels AND o.ordered_at >= :start_date AND o.ordered_at <= :end_date
 AND o.gross_amount >= :min_order"""
CTE = (
    """WITH line_totals AS (
 SELECT order_id,SUM(quantity*unit_price) gross_amount,SUM(quantity*unit_cost) cost_amount
 FROM order_lines GROUP BY order_id
), scoped AS (
 SELECT o.*,c.name customer,c.region,c.segment,r.name representative,t.gross_amount,t.cost_amount
 FROM orders o JOIN customers c ON c.id=o.customer_id
 JOIN representatives r ON r.id=c.rep_id JOIN line_totals t ON t.order_id=o.id
), filtered AS (SELECT o.*,c.region customer_region FROM scoped o JOIN customers c ON c.id=o.customer_id """
    + FILTER
    + """), ranked AS (
 SELECT *,gross_amount-cost_amount margin,
 ROW_NUMBER() OVER (PARTITION BY region ORDER BY gross_amount DESC,id) market_rank FROM filtered
) """
)


def sales_code(select):
    return (
        """def generate_df(ds_clients, excel_files, params):
    client = ds_clients[next(iter(ds_clients))]
    period = params.get("period") or {}
    bindings = {"region": params.get("region"), "channels": params.get("channels") or [],
                "start_date": period.get("from") or "1900-01-01", "end_date": period.get("to") or "2100-01-01",
                "min_order": params.get("min_order") or 0}
    return client.execute_query("""
        + repr(CTE + select)
        + """, params=bindings)
"""
    )


APP_DEFS = {
    "commerce": {
        "title": "Commerce Workbench",
        "brief": "Build a polished commerce exploration app. Stable title Commerce Workbench. Main workspace is weekly revenue/margin analysis with market comparison and customer drill-down. Use quiet ink/navy, warm white, a restrained teal accent, clean sans typography. No oversized numeric headline or mandatory KPI row. Include period date range, region, multi-channel selection and min_order numeric controls using the exact manifest; use an explicit Apply for grouped changes. Provide customer selection/detail and a table/chart view where useful. All displayed totals must come from aggregated query rows, not the sampled customer table. Make the embedded 960px workspace beautiful and useful.",
        "queries": [
            (
                "Weekly performance",
                "SELECT strftime('%Y-%W',ordered_at) week,ROUND(SUM(gross_amount),2) revenue,ROUND(SUM(margin),2) margin,COUNT(*) orders FROM ranked GROUP BY week ORDER BY week",
            ),
            (
                "Market mix",
                "SELECT region,channel,ROUND(SUM(gross_amount),2) revenue,COUNT(*) orders FROM ranked GROUP BY region,channel ORDER BY revenue DESC",
            ),
            (
                "Customer portfolio",
                "SELECT customer_id,customer,region,segment,representative,COUNT(*) orders,ROUND(SUM(gross_amount),2) revenue,ROUND(SUM(margin),2) margin,MAX(ordered_at) last_order FROM ranked GROUP BY customer_id ORDER BY revenue DESC",
            ),
        ],
    },
    "reps": {
        "title": "Revenue Operations",
        "brief": "Build a refined, dense revenue operations data app for inspecting representative performance. Stable title Revenue Operations. Main surface: ranked representatives with revenue, margin, customers and a selectable detail panel; a supporting weekly view may be another tab. Use precise typography, muted indigo, understated borders and status styling backed by data. Controls must cover region, channels, period, and min_order via real backend parameters. Keep selected representative stable across reruns; clear it when absent. Implement useful responsive navigation, empty/errors/loading, and source inspectability. No giant total above the workspace.",
        "queries": [
            (
                "Representative performance",
                "SELECT representative,region,COUNT(DISTINCT customer_id) customers,COUNT(*) orders,ROUND(SUM(gross_amount),2) revenue,ROUND(SUM(margin),2) margin FROM ranked GROUP BY representative,region ORDER BY revenue DESC",
            ),
            (
                "Weekly performance",
                "SELECT strftime('%Y-%W',ordered_at) week,representative,ROUND(SUM(gross_amount),2) revenue FROM ranked GROUP BY week,representative ORDER BY week,representative",
            ),
            (
                "Priority accounts",
                "SELECT customer_id,customer,representative,region,COUNT(*) orders,ROUND(SUM(gross_amount),2) revenue FROM ranked GROUP BY customer_id ORDER BY revenue DESC",
            ),
        ],
    },
    "catalog": {
        "title": "Listening Room",
        "brief": "Build an exceptional music catalog exploration data app called Listening Room. Lead with searchable albums/tracks and a selected album/detail workspace, not revenue or KPI cards. A warm paper background, dark ink, restrained rust/terracotta accents, elegant but legible typography and editorial album rows are appropriate. No invented images; use typography and geometric album marks if desired. Backend controls genre, max_price, search, and min_minutes must use useParams and stable options; provide search, clear/reset, track details, sorting, loading/errors/empty states. Filters/search query the full backend catalog; local selections are separate. Do not pretend to play audio. Support narrow 400px and embedded 960px views.",
        "queries": [],
    },
}


async def seed():
    # Deterministic external source fixture; the app's own DB is seeded via API.
    source = session["sqlite_sources"][0]
    ds = source["id"]
    dbfile = Path(source.get("database") or Path(args.session).parent / "unused")
    source_info = api("GET", f"/api/data_sources/{ds}")
    config = source_info.get("config") or {}
    config = json.loads(config) if isinstance(config, str) else config
    if not config.get("database"):
        raise RuntimeError(
            "The seeded SQLite source must expose its local database path"
        )
    dbfile = Path(config["database"])
    db = sqlite3.connect(dbfile)
    db.executescript("""CREATE TABLE IF NOT EXISTS representatives(id INTEGER PRIMARY KEY,name TEXT);
 CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY,name TEXT,region TEXT,segment TEXT,rep_id INTEGER);
 CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,customer_id INTEGER,ordered_at TEXT,channel TEXT);
 CREATE TABLE IF NOT EXISTS order_lines(id INTEGER PRIMARY KEY,order_id INTEGER,product_id INTEGER,quantity INTEGER,unit_price REAL,unit_cost REAL);
 CREATE TABLE IF NOT EXISTS albums(id INTEGER PRIMARY KEY,title TEXT,artist TEXT,genre TEXT,year INTEGER);
 CREATE TABLE IF NOT EXISTS tracks(id INTEGER PRIMARY KEY,album_id INTEGER,title TEXT,minutes REAL,price REAL);
 """)
    rng = random.Random(1129)
    reps = [
        "Maya Chen",
        "Theo Brooks",
        "Amina Diallo",
        "Lucas Costa",
        "Noor Haddad",
        "Sofia Rossi",
    ]
    db.executemany(
        "INSERT OR REPLACE INTO representatives VALUES (?,?)", enumerate(reps, 1)
    )
    brands = [
        "Aster",
        "Northstar",
        "Meridian",
        "Juniper",
        "Atlas",
        "Cedar",
        "Harbor",
        "Orbit",
        "Cobalt",
        "Lumen",
        "Fieldwork",
        "Solstice",
    ]
    customers = [
        (
            i,
            f"{brands[(i - 1) % 12]} {'Labs' if i % 2 else 'Studio'} {i:02}",
            REGIONS[(i - 1) % 4],
            ["Enterprise", "Growth", "Startup"][i % 3],
            (i - 1) % 6 + 1,
        )
        for i in range(1, 49)
    ]
    db.executemany("INSERT OR REPLACE INTO customers VALUES (?,?,?,?,?)", customers)
    orders = []
    lines = []
    lid = 0
    for i in range(1, 1801):
        orders.append(
            (
                i,
                rng.randint(1, 48),
                (date(2026, 1, 1) + timedelta(days=rng.randrange(181))).isoformat(),
                CHANNELS[rng.randrange(3)],
            )
        )
        for j in range(rng.randint(1, 4)):
            lid += 1
            price = rng.choice([49, 79, 129, 249, 499])
            lines.append(
                (
                    lid,
                    i,
                    rng.randrange(1, 9),
                    rng.randint(1, 8),
                    price,
                    round(price * rng.uniform(0.4, 0.75), 2),
                )
            )
    db.executemany("INSERT OR REPLACE INTO orders VALUES (?,?,?,?)", orders)
    db.executemany("INSERT OR REPLACE INTO order_lines VALUES (?,?,?,?,?,?)", lines)
    titles = [
        "Blue Hours",
        "Glass Islands",
        "Soft Geometry",
        "Night Letters",
        "Open Water",
        "Quiet Machines",
        "Golden Thread",
        "Paper Satellites",
        "After the Rain",
        "Northern Light",
        "Slow Current",
        "City Gardens",
    ]
    artists = [
        "Ada Vale",
        "Miles Arden",
        "Sora Fields",
        "The Low Tides",
        "Juniper Quartet",
        "Ember Coast",
    ]
    genres = ["Jazz", "Electronic", "Indie", "Ambient"]
    albums = [
        (
            i,
            titles[(i - 1) % 12] + (" / II" if i > 12 else ""),
            artists[(i - 1) % 6],
            genres[(i - 1) % 4],
            2020 + i % 6,
        )
        for i in range(1, 25)
    ]
    tracks = [
        (
            i,
            (i - 1) // 6 + 1,
            [
                "First Light",
                "Small Hours",
                "Moving Lines",
                "Between Places",
                "Still Here",
                "Homeward",
            ][(i - 1) % 6],
            round(2 + rng.random() * 6, 2),
            [0.99, 1.29, 1.99][i % 3],
        )
        for i in range(1, 145)
    ]
    db.executemany("INSERT OR REPLACE INTO albums VALUES (?,?,?,?,?)", albums)
    db.executemany("INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?)", tracks)
    db.commit()
    db.close()
    manifest = (
        json.loads((out / "manifest.json").read_text())
        if (out / "manifest.json").exists()
        else {}
    )
    for slug, definition in APP_DEFS.items():
        if args.app not in ("all", slug):
            continue
        report = api(
            "POST",
            "/api/reports",
            json={"title": definition["title"], "data_sources": [ds]},
        )
        defs = [
            (title, sales_code(sql), PARAMS) for title, sql in definition["queries"]
        ]
        if slug == "catalog":
            params = [
                {
                    "name": "genre",
                    "type": "string",
                    "label": "Genre",
                    "options": genres,
                    "strict_options": True,
                },
                {"name": "search", "type": "string", "label": "Search", "default": ""},
                {
                    "name": "max_price",
                    "type": "number",
                    "label": "Maximum price",
                    "default": 2,
                },
                {
                    "name": "min_minutes",
                    "type": "number",
                    "label": "Minimum duration",
                    "default": 0,
                },
            ]
            base = " FROM tracks t JOIN albums a ON a.id=t.album_id WHERE (:genre IS NULL OR a.genre=:genre) AND (a.title LIKE :search OR a.artist LIKE :search OR t.title LIKE :search) AND t.price<=:max_price AND t.minutes>=:min_minutes"
            for title, sql in [
                (
                    "Album collection",
                    "SELECT a.id album_id,a.title album,a.artist,a.genre,a.year,COUNT(*) tracks,ROUND(SUM(t.minutes),1) minutes,ROUND(SUM(t.price),2) price"
                    + base
                    + " GROUP BY a.id ORDER BY a.artist,a.title",
                ),
                (
                    "Track details",
                    "SELECT t.id track_id,t.album_id,t.title track,a.title album,a.artist,a.genre,t.minutes,t.price"
                    + base
                    + " ORDER BY a.title,t.id",
                ),
            ]:
                code = (
                    'def generate_df(ds_clients, excel_files, params):\n    p = {"genre": params.get("genre"), "max_price": params.get("max_price"), "min_minutes": params.get("min_minutes")}\n    p["search"] = "%" + (params.get("search") or "") + "%"\n    return ds_clients[next(iter(ds_clients))].execute_query('
                    + repr(sql)
                    + ", params=p)\n"
                )
                defs.append((title, code, params))
        queries = []
        for title, code, params in defs:
            q = api(
                "POST", "/api/queries", json={"title": title, "report_id": report["id"]}
            )
            run = api(
                "POST",
                f"/api/queries/{q['id']}/run",
                json={
                    "mode": "builder",
                    "code": code,
                    "parameters": params,
                    "params": {p["name"]: p.get("default") for p in params},
                    "row_limit": 10000,
                    "type": "table",
                },
            )
            step = run.get("step", run)
            if step.get("status") != "success":
                raise RuntimeError(str(run)[:1600])
            queries.append(
                {
                    "id": q["id"],
                    "title": title,
                    "parameters": params,
                    "code": code,
                    "rows": step.get("data", {}).get("rows", []),
                    "columns": step.get("data", {}).get("columns", []),
                }
            )
            print(slug, title, "rows", len(queries[-1]["rows"]), flush=True)
        # Only the visualizations have no creation HTTP API; use the production ORM.
        import main
        from app.dependencies import async_session_maker
        from app.models.visualization import Visualization

        async with async_session_maker() as db:
            for q in queries:
                v = Visualization(
                    title=q["title"],
                    status="success",
                    report_id=report["id"],
                    query_id=q["id"],
                    view={"type": "table"},
                )
                db.add(v)
                await db.flush()
                q["viz_id"] = str(v.id)
            await db.commit()
        manifest[slug] = {
            "report_id": report["id"],
            "title": definition["title"],
            "brief": definition["brief"],
            "queries": queries,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("Seed complete", flush=True)


async def install_fixtures():
    """Replay reviewed synthetic app sources through the real artifact API; no LLM."""
    manifest = json.loads((out / "manifest.json").read_text())
    fixture_dir = Path(__file__).parent / "fixtures/data_apps"
    for slug, app in manifest.items():
        if args.app not in ("all", slug):
            continue
        code = (fixture_dir / (slug + ".jsx")).read_text()
        for i, q in enumerate(app["queries"]):
            code = code.replace(f"FIXTURE_VIZ_{i}", q["viz_id"])
        artifact = api(
            "POST",
            "/api/artifacts",
            json={
                "report_id": app["report_id"],
                "title": app["title"],
                "mode": "page",
                "content": {
                    "code": code,
                    "visualization_ids": [q["viz_id"] for q in app["queries"]],
                    "runtime_version": 11,
                },
            },
        )
        api(
            "PUT",
            "/api/reports/" + app["report_id"] + "/visibility/artifact",
            json={"visibility": "internal", "run_identity": "viewer"},
        )
        app["artifact_id"] = artifact["id"]
        print(slug, "http://localhost:3000/r/" + app["report_id"])
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))


async def generate():
    import main
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.models.user import User
    from app.models.organization import Organization
    from app.models.llm_model import LLMModel
    from app.ai.llm import LLM
    from app.ai.llm.types import Message, TextDeltaEvent
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    manifest = json.loads((out / "manifest.json").read_text())
    for slug, app in manifest.items():
        if args.app not in ("all", slug):
            continue
        async with async_session_maker() as db:
            model = (
                (
                    await db.execute(
                        select(LLMModel).where(
                            LLMModel.organization_id == session["organization"]["id"],
                            LLMModel.is_default == True,
                        )
                    )
                )
                .scalars()
                .unique()
                .one()
            )
            report = await db.get(Report, app["report_id"])
            user = await db.get(User, report.user_id)
            org = await db.get(Organization, report.organization_id)
            tool = CreateArtifactTool()
            llm = LLM(model, usage_session_maker=async_session_maker)
            payload = [
                {
                    "id": q["viz_id"],
                    "query_id": q["id"],
                    "title": q["title"],
                    "parameters": q["parameters"],
                    "columns": q["columns"],
                    "rows": q["rows"][:8],
                    "row_count": len(q["rows"]),
                }
                for q in app["queries"]
            ]
            prompt = (
                app["brief"]
                + "\n\nAvailable source-backed datasets (rows shown are samples; runtime contains the full query result):\n"
                + json.dumps(payload)
                + "\nReturn only complete artifact JSX, no markdown commentary. Every declared adjustable parameter needs a real control. Use numeric values for number parameters and a {from,to} object for period. All dataset IDs must be bound somewhere in the app. Ensure the initial state is useful."
            )
            code = ""
            start = time.monotonic()
            async for e in llm.inference_stream_v2(
                messages=[Message(role="user", content=prompt)],
                system=tool._build_page_system_prompt(),
                usage_scope="data_app_sandbox",
            ):
                if isinstance(e, TextDeltaEvent):
                    code += e.text
            code = tool._extract_code(code)
            (out / (slug + "-generated.jsx")).write_text(code)
            print(
                slug,
                "generated",
                len(code),
                "chars",
                round(time.monotonic() - start, 1),
                "seconds",
                flush=True,
            )
            async for event in tool.run_stream(
                {
                    "title": app["title"],
                    "prompt": app["brief"],
                    "mode": "page",
                    "visualization_ids": [q["viz_id"] for q in app["queries"]],
                    "code": code,
                },
                {
                    "db": db,
                    "report": report,
                    "user": user,
                    "organization": org,
                    "model": model,
                },
            ):
                if event.type == "tool.end":
                    observation = event.payload.get("observation", {})
                    output = event.payload.get("output", {})
                    app["artifact_id"] = output.get("artifact_id") or observation.get(
                        "artifact_id"
                    )
                    app["generation_seconds"] = round(time.monotonic() - start, 1)
                    app["model"] = model.model_id
                    summary = {
                        k: v for k, v in event.payload.items() if k not in ("images",)
                    }
                    (out / (slug + "-result.json")).write_text(
                        json.dumps(summary, default=str)
                    )
                    print(
                        slug,
                        "artifact",
                        app["artifact_id"],
                        "error",
                        observation.get("error"),
                        flush=True,
                    )
            latest = json.loads((out / "manifest.json").read_text())
            latest[slug] = app
            (out / "manifest.json").write_text(json.dumps(latest, indent=2))


asyncio.run(
    {"seed": seed, "generate": generate, "install-fixtures": install_fixtures}[
        args.action
    ]()
)
