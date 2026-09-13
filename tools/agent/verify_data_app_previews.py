"""Production preview/thumbnail bootstrap parity, including pre-fix reproduction."""

import argparse, ast, asyncio, json, subprocess
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright
from app.ai.tools.implementations.create_artifact import CreateArtifactTool
from app.services.thumbnail_service import ThumbnailService
from app.services.artifact_libs import get_inline_scripts

p = argparse.ArgumentParser()
p.add_argument("--baseline", action="store_true")
a = p.parse_args()
root = Path(__file__).resolve().parents[2]
CODE = '<script type="text/babel">function App(){return <h1>{fmt(0.37,{pct:true})}</h1>} ReactDOM.createRoot(document.getElementById("root")).render(<App/>);</script>'


def renderer(cls, file):
    if not a.baseline:
        return cls()
    src = subprocess.check_output(["git", "show", "3e32c09db2e88815bd2b9910db84ddd71d145d7f:" + file], cwd=root, text=True)
    tree = ast.parse(src)
    node = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls.__name__
    )
    node.body = [
        n
        for n in node.body
        if isinstance(n, ast.FunctionDef) and n.name == "_build_thumbnail_html"
    ]
    node.bases = []
    scope = {
        "json": json,
        "Optional": Optional,
        "get_inline_scripts": get_inline_scripts,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
            file,
            "exec",
        ),
        scope,
    )
    return scope[cls.__name__]()


async def run():
    create = renderer(
        CreateArtifactTool, "backend/app/ai/tools/implementations/create_artifact.py"
    )
    thumb = renderer(ThumbnailService, "backend/app/services/thumbnail_service.py")
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for version in [0, 11]:
            data = {
                "report": {"id": "fixture"},
                "visualizations": [],
                "runtime": {"version": version},
            }
            for name, html in [
                ("preview", create._build_thumbnail_html(data, CODE)),
                (
                    "thumbnail",
                    thumb._build_thumbnail_html(
                        "fixture", "Fixture", None, CODE, [], runtime_version=version
                    ),
                ),
            ]:
                page = await browser.new_page()
                await page.set_content(html, wait_until="load")
                await page.locator("h1").wait_for()
                actual = await page.locator("h1").inner_text()
                expected = "37.0%" if version == 11 else "0.4%"
                results.append(
                    {
                        "renderer": name,
                        "version": version,
                        "expected": expected,
                        "actual": actual,
                        "pass": actual == expected,
                    }
                )
                await page.close()
        await browser.close()
    print(json.dumps(results, indent=2))
    assert all(r["pass"] for r in results), (
        "Version metadata must precede shared runtime initialization"
    )


asyncio.run(run())
