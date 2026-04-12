"""Excel Add-in routes.

Serves the Office manifest.xml (with dynamic base URL) and the taskpane HTML
so that on-prem customers can sideload the add-in directly from their BOW
instance — no external plugin host required.

Routes (behind Nuxt `/excel` → `/api/excel` proxy):
    GET /excel/manifest.xml   → dynamic Office manifest
    GET /excel/taskpane.html  → self-contained taskpane page

Icon assets are served statically by Nuxt from frontend/public/icons/excel/.
"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.settings.config import settings

router = APIRouter(prefix="/excel", tags=["excel"])


def _get_base_url(request: Request) -> str:
    """Return the externally-reachable base URL for this BOW instance.

    Priority:
      1. bow_config.base_url (operator-configured, authoritative)
      2. Infer from the incoming request (fallback for dev)
    """
    configured = (settings.bow_config.base_url or "").rstrip("/")
    if configured and configured not in ("http://0.0.0.0:3000",):
        return configured
    # Fallback: build from request headers (works behind reverse proxies that
    # set X-Forwarded-{Proto,Host}).
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    return f"{scheme}://{host}"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_MANIFEST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<OfficeApp xmlns="http://schemas.microsoft.com/office/appforoffice/1.1"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
           xmlns:bt="http://schemas.microsoft.com/office/officeappbasictypes/1.0"
           xsi:type="TaskPaneApp">
  <Id>5a276b40-1c05-4b22-921f-db43fa0e5590</Id>
  <Version>1.0.0.0</Version>
  <ProviderName>Bag of Words</ProviderName>
  <DefaultLocale>en-US</DefaultLocale>
  <DisplayName DefaultValue="Bag of Words"/>
  <Description DefaultValue="Chat with your data and paste AI-generated analysis directly into Excel. Bag of Words is an agentic analytics platform that connects to your databases and warehouses, understands your data, and returns results you can drop straight into a spreadsheet."/>
  <IconUrl DefaultValue="{base_url}/icons/excel/icon-64.png"/>
  <HighResolutionIconUrl DefaultValue="{base_url}/icons/excel/icon-128.png"/>
  <SupportUrl DefaultValue="https://docs.bagofwords.com"/>
  <AppDomains>
    <AppDomain>{base_url}</AppDomain>
    <AppDomain>https://docs.bagofwords.com</AppDomain>
  </AppDomains>
  <Hosts>
    <Host Name="Workbook"/>
  </Hosts>
  <DefaultSettings>
    <SourceLocation DefaultValue="{base_url}/excel/taskpane.html"/>
  </DefaultSettings>
  <Permissions>ReadWriteDocument</Permissions>
  <VersionOverrides xmlns="http://schemas.microsoft.com/office/taskpaneappversionoverrides/1.0"
                    xsi:type="VersionOverridesV1_0">
    <Hosts>
      <Host xsi:type="Workbook">
        <DesktopFormFactor>
          <GetStarted>
            <Title resid="GetStarted.Title"/>
            <Description resid="GetStarted.Description"/>
            <LearnMoreUrl resid="GetStarted.LearnMoreUrl"/>
          </GetStarted>
          <FunctionFile resid="Commands.Url"/>
          <ExtensionPoint xsi:type="PrimaryCommandSurface">
            <OfficeTab id="TabHome">
              <Group id="BowCommandsGroup">
                <Label resid="BowCommandsGroup.Label"/>
                <Icon>
                  <bt:Image size="16" resid="Icon.16x16"/>
                  <bt:Image size="32" resid="Icon.32x32"/>
                  <bt:Image size="80" resid="Icon.80x80"/>
                </Icon>
                <Control xsi:type="Button" id="BowTaskpaneButton">
                  <Label resid="BowTaskpaneButton.Label"/>
                  <Supertip>
                    <Title resid="BowTaskpaneButton.Label"/>
                    <Description resid="BowTaskpaneButton.Tooltip"/>
                  </Supertip>
                  <Icon>
                    <bt:Image size="16" resid="Icon.16x16"/>
                    <bt:Image size="32" resid="Icon.32x32"/>
                    <bt:Image size="80" resid="Icon.80x80"/>
                  </Icon>
                  <Action xsi:type="ShowTaskpane">
                    <TaskpaneId>BowTaskpaneId</TaskpaneId>
                    <SourceLocation resid="Taskpane.Url"/>
                  </Action>
                </Control>
              </Group>
            </OfficeTab>
          </ExtensionPoint>
        </DesktopFormFactor>
      </Host>
    </Hosts>
    <Resources>
      <bt:Images>
        <bt:Image id="Icon.16x16" DefaultValue="{base_url}/icons/excel/icon-16.png"/>
        <bt:Image id="Icon.32x32" DefaultValue="{base_url}/icons/excel/icon-32.png"/>
        <bt:Image id="Icon.80x80" DefaultValue="{base_url}/icons/excel/icon-80.png"/>
      </bt:Images>
      <bt:Urls>
        <bt:Url id="GetStarted.LearnMoreUrl" DefaultValue="https://docs.bagofwords.com"/>
        <bt:Url id="Commands.Url" DefaultValue="{base_url}/excel/commands.html"/>
        <bt:Url id="Taskpane.Url" DefaultValue="{base_url}/excel/taskpane.html"/>
      </bt:Urls>
      <bt:ShortStrings>
        <bt:String id="GetStarted.Title" DefaultValue="Welcome to Bag of Words for Excel"/>
        <bt:String id="BowCommandsGroup.Label" DefaultValue="BOW"/>
        <bt:String id="BowTaskpaneButton.Label" DefaultValue="BOW"/>
      </bt:ShortStrings>
      <bt:LongStrings>
        <bt:String id="GetStarted.Description" DefaultValue="Open the Bag of Words task pane to chat with your data and paste AI-generated analysis into your spreadsheet."/>
        <bt:String id="BowTaskpaneButton.Tooltip" DefaultValue="Open Bag of Words and bring AI analysis into your spreadsheet."/>
      </bt:LongStrings>
    </Resources>
  </VersionOverrides>
</OfficeApp>"""


@router.api_route("/manifest.xml", methods=["GET", "HEAD"])
async def get_manifest(request: Request):
    base_url = _get_base_url(request)
    xml = _MANIFEST_TEMPLATE.replace("{base_url}", base_url)
    return Response(content=xml, media_type="application/xml")


_COMMANDS_HTML = """\
<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<script src="https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js"></script>
</head><body></body></html>"""


@router.api_route("/commands.html", methods=["GET", "HEAD"])
async def get_commands(request: Request):
    return Response(content=_COMMANDS_HTML, media_type="text/html")


# ---------------------------------------------------------------------------
# Taskpane  — self-contained HTML with inline JS (no webpack needed)
# ---------------------------------------------------------------------------

def _build_taskpane_html(base_url: str) -> str:
    """Return the taskpane HTML with the app URL baked in."""
    app_url = base_url + "?excel=true"
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta http-equiv="X-UA-Compatible" content="IE=Edge" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Bag of Words</title>
    <script type="text/javascript" src="https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js"></script>
    <style>
        html, body {{ margin: 0; padding: 0; width: 100%; height: 100%;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            font-size: 14px; color: #1f2937; background: #fff; }}
        #web-app-frame {{ display: block; width: 100%; height: 100vh; border: none; background: #fff; }}
        .overlay {{ position: fixed; inset: 0; display: flex; align-items: center;
            justify-content: center; padding: 24px; background: #fff; z-index: 10; }}
        .panel {{ width: 100%; max-width: 320px; }}
        .panel h2 {{ margin: 0 0 8px; font-size: 16px; font-weight: 600; }}
        .panel p {{ margin: 0 0 16px; font-size: 13px; color: #4b5563; line-height: 1.5; }}
        .panel label {{ display: block; font-size: 12px; font-weight: 600; color: #374151; margin-bottom: 6px; }}
        .panel input[type="url"] {{ width: 100%; box-sizing: border-box; padding: 8px 10px;
            font-size: 13px; border: 1px solid #d1d5db; border-radius: 4px; outline: none; }}
        .panel input[type="url"]:focus {{ border-color: #2563eb; box-shadow: 0 0 0 2px rgba(37,99,235,.2); }}
        .button-row {{ display: flex; gap: 8px; margin-top: 16px; }}
        .panel button {{ flex: 1; padding: 8px 12px; font-size: 13px; font-weight: 600;
            border-radius: 4px; border: 1px solid transparent; cursor: pointer; }}
        .panel button.primary {{ background: #2563eb; color: #fff; border-color: #2563eb; }}
        .panel button.primary:hover {{ background: #1d4ed8; }}
        .panel button.secondary {{ background: #fff; color: #374151; border-color: #d1d5db; }}
        .panel button.secondary:hover {{ background: #f3f4f6; }}
        .panel .error-text {{ color: #b91c1c; }}
        #settings-toggle {{ position: fixed; top: 8px; right: 8px; width: 28px; height: 28px; padding: 0;
            border: 1px solid #e5e7eb; border-radius: 4px; background: rgba(255,255,255,.9);
            cursor: pointer; font-size: 14px; line-height: 1; color: #4b5563; z-index: 20; }}
        #settings-toggle:hover {{ background: #f3f4f6; }}
        [hidden] {{ display: none !important; }}
    </style>
</head>
<body>
    <iframe id="web-app-frame" title="Bag of Words"></iframe>
    <button type="button" id="settings-toggle" title="Configure Bag of Words URL"
            aria-label="Configure Bag of Words URL">&#9881;</button>
    <div class="overlay" id="error-overlay" hidden>
        <div class="panel">
            <h2 class="error-text">Can&rsquo;t reach Bag of Words</h2>
            <p id="error-message">We couldn&rsquo;t load the Bag of Words web app.</p>
            <div class="button-row">
                <button type="button" class="secondary" id="error-configure">Configure URL</button>
                <button type="button" class="primary" id="error-retry">Retry</button>
            </div>
        </div>
    </div>
    <div class="overlay" id="settings-overlay" hidden>
        <form class="panel" id="settings-form">
            <h2>Bag of Words URL</h2>
            <p>Point the add-in at your Bag of Words instance.</p>
            <label for="settings-url-input">URL</label>
            <input type="url" id="settings-url-input" required placeholder="{base_url}" />
            <div class="button-row">
                <button type="button" class="secondary" id="settings-cancel">Cancel</button>
                <button type="submit" class="primary">Save</button>
            </div>
        </form>
    </div>
<script>
(function() {{
  var DEFAULT_APP_URL = "{app_url}";
  var STORAGE_KEY = "bow-app-url";
  var MAX_SELECTION_CELLS = 1000;

  function normalizeUrl(v) {{
    if (!v) return null;
    try {{ var u = new URL(v); if (u.protocol !== "https:" && u.protocol !== "http:") return null;
      var p = u.pathname === "/" ? "" : u.pathname.replace(/\\/$/, ""); return u.origin + p + u.search;
    }} catch(e) {{ return null; }}
  }}
  function originOf(v) {{ try {{ return new URL(v).origin; }} catch(e) {{ return null; }} }}

  function resolveAppUrl() {{
    try {{ var p = new URLSearchParams(window.location.search); var q = normalizeUrl(p.get("app"));
      if (q) return q; }} catch(e) {{}}
    try {{ var s = normalizeUrl(window.localStorage.getItem(STORAGE_KEY));
      if (s) return s; }} catch(e) {{}}
    return normalizeUrl(DEFAULT_APP_URL) || DEFAULT_APP_URL;
  }}
  function saveAppUrl(v) {{
    var n = normalizeUrl(v); if (!n) return null;
    try {{ window.localStorage.setItem(STORAGE_KEY, n); }} catch(e) {{}} return n;
  }}

  var currentAppUrl = resolveAppUrl();
  var currentAppOrigin = originOf(currentAppUrl);

  function getIframe() {{ return document.getElementById("web-app-frame"); }}
  function postToApp(msg) {{
    var f = getIframe(); if (!f || !f.contentWindow || !currentAppOrigin) return;
    try {{ f.contentWindow.postMessage(msg, currentAppOrigin); }} catch(e) {{}}
  }}
  function showOverlay(id) {{ var el = document.getElementById(id); if (el) el.hidden = false; }}
  function hideOverlay(id) {{ var el = document.getElementById(id); if (el) el.hidden = true; }}
  function openSettings() {{
    var inp = document.getElementById("settings-url-input"); if (inp) inp.value = currentAppUrl || "";
    showOverlay("settings-overlay");
  }}
  function closeSettings() {{ hideOverlay("settings-overlay"); }}
  function loadApp(url) {{
    currentAppUrl = url; currentAppOrigin = originOf(url); hideOverlay("error-overlay");
    var f = getIframe(); if (f) f.src = url;
  }}
  function showError(msg) {{
    var el = document.getElementById("error-message"); if (el && msg) el.textContent = msg;
    showOverlay("error-overlay");
  }}

  function wireSettingsUi() {{
    var toggle = document.getElementById("settings-toggle");
    var cancel = document.getElementById("settings-cancel");
    var form = document.getElementById("settings-form");
    var retry = document.getElementById("error-retry");
    var configure = document.getElementById("error-configure");
    if (toggle) toggle.addEventListener("click", openSettings);
    if (cancel) cancel.addEventListener("click", closeSettings);
    if (configure) configure.addEventListener("click", openSettings);
    if (retry) retry.addEventListener("click", function() {{ loadApp(currentAppUrl); }});
    if (form) form.addEventListener("submit", function(e) {{
      e.preventDefault(); var inp = document.getElementById("settings-url-input"); if (!inp) return;
      var saved = saveAppUrl(inp.value);
      if (!saved) {{ inp.setCustomValidity("Please enter a valid URL (https://...)"); inp.reportValidity(); return; }}
      inp.setCustomValidity(""); closeSettings(); loadApp(saved);
    }});
  }}

  function wireIframeErrorHandling() {{
    var f = getIframe(); if (!f) return; var loaded = false;
    f.addEventListener("load", function() {{
      loaded = true; hideOverlay("error-overlay");
      setTimeout(function() {{ postToApp({{ type: "excelInitialized" }}); }}, 500);
      setInterval(function() {{ postToApp({{ type: "excelInitialized" }}); }}, 5000);
    }});
    f.addEventListener("error", function() {{
      showError("Couldn\\u2019t load Bag of Words from " + currentAppUrl);
    }});
    setTimeout(function() {{ if (!loaded) showError("Loading is taking longer than expected."); }}, 15000);
  }}

  function handleMessageFromApp(event) {{
    if (!event || !currentAppOrigin || event.origin !== currentAppOrigin) return;
    var data = event.data; if (!data || typeof data !== "object") return;
    if (data.type === "applyToExcel") {{
      try {{ var payload = typeof data.data === "string" ? JSON.parse(data.data) : data.data;
        appendDataToExcel(payload); }} catch(e) {{}}
    }}
  }}

  function appendDataToExcel(data) {{
    Excel.run(function(ctx) {{
      var range = ctx.workbook.getSelectedRange();
      range.load(["address","rowIndex","columnIndex"]); return ctx.sync().then(function() {{
        var colDefs, rows;
        if (data && data.widget && data.widget.last_step && data.widget.last_step.data) {{
          colDefs = data.widget.last_step.data.columns; rows = data.widget.last_step.data.rows;
        }} else return;
        var headers = colDefs.map(function(c) {{ return c.headerName || c.field || ""; }});
        var values = [headers];
        rows.forEach(function(row) {{
          values.push(colDefs.map(function(c) {{
            var field = c.field || c.colId || c.headerName; var v = row[field];
            if (v === undefined || v === null) return "";
            return typeof v === "object" ? JSON.stringify(v) : v;
          }}));
        }});
        var sheet = range.worksheet; var startRow = range.rowIndex; var startCol = range.columnIndex;
        var target = sheet.getRangeByIndexes(startRow, startCol, values.length, headers.length);
        target.values = values;
        var headerRange = sheet.getRangeByIndexes(startRow, startCol, 1, headers.length);
        headerRange.format.fill.color = "#ADD8E6"; headerRange.format.font.bold = true;
        ["EdgeTop","EdgeBottom","EdgeLeft","EdgeRight","InsideHorizontal","InsideVertical"].forEach(function(b) {{
          target.format.borders.getItem(b).style = "Continuous";
          target.format.borders.getItem(b).weight = "Thin";
        }});
        target.getEntireColumn().format.autofitColumns();
        return ctx.sync();
      }});
    }}).catch(function(e) {{ console.error("Excel.run error:", e); }});
  }}

  function handleSelectionChange() {{
    Excel.run(function(ctx) {{ return sendCurrentSelection(ctx); }});
  }}

  function sendCurrentSelection(ctx) {{
    var range = ctx.workbook.getSelectedRange();
    range.load(["address","values","rowCount","columnCount"]);
    var sheet = range.worksheet; sheet.load("name");
    return ctx.sync().then(function() {{
      var cellCount = range.rowCount * range.columnCount;
      var vals = range.values; var truncated = false;
      if (cellCount > MAX_SELECTION_CELLS) {{
        var maxRows = Math.max(1, Math.floor(MAX_SELECTION_CELLS / range.columnCount));
        vals = vals.slice(0, maxRows); truncated = true;
      }}
      postToApp({{ type: "cellSelected", address: range.address, sheetName: sheet.name,
        selectionValues: vals, cellCount: Math.min(cellCount, MAX_SELECTION_CELLS),
        totalCellCount: cellCount, truncated: truncated,
        rowCount: range.rowCount, columnCount: range.columnCount }});
    }});
  }}

  Office.onReady(function(info) {{
    wireSettingsUi(); wireIframeErrorHandling(); loadApp(currentAppUrl);
    if (info.host !== Office.HostType.Excel) return;
    window.addEventListener("message", handleMessageFromApp);
    Excel.run(function(ctx) {{
      ctx.workbook.worksheets.onSelectionChanged.add(handleSelectionChange);
      return ctx.sync().then(function() {{ return sendCurrentSelection(ctx); }});
    }}).catch(function(e) {{ console.error("Failed to register Excel selection listener:", e); }});
  }});
}})();
</script>
</body>
</html>"""


@router.api_route("/taskpane.html", methods=["GET", "HEAD"])
async def get_taskpane(request: Request):
    base_url = _get_base_url(request)
    html = _build_taskpane_html(base_url)
    return Response(content=html, media_type="text/html")
