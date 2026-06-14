"""Seed a rich Knowledge/Instructions demo: many agents + many instructions
with varied settings (status, load mode, source, category, scope/reach).

Run against a LIVE local backend (python main.py) on :8000.
    python scripts/seed_knowledge_demo.py
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error

BASE = os.environ.get("BOW_SEED_BASE", "http://localhost:8000")
EMAIL = "sandbox@bow.dev"
PASSWORD = "Sandbox123!"
CHINOOK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "demo-datasources", "chinook.sqlite"))

TOKEN = None
ORG_ID = None


def req(method, path, body=None, form=False, auth=True):
    url = BASE + path
    headers = {}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if auth and TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if auth and ORG_ID:
        headers["X-Organization-Id"] = ORG_ID
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


import urllib.parse  # noqa: E402


def wait_for_server():
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/settings", timeout=3):
                return True
        except Exception:
            time.sleep(1)
    return False


def setup_auth():
    global TOKEN, ORG_ID
    req("POST", "/api/auth/register", {"email": EMAIL, "password": PASSWORD, "name": "Sandbox Admin"}, auth=False)
    st, data = req("POST", "/api/auth/jwt/login", {"username": EMAIL, "password": PASSWORD}, form=True, auth=False)
    assert data and data.get("access_token"), f"login failed: {st} {data}"
    TOKEN = data["access_token"]
    st, orgs = req("GET", "/api/organizations")
    assert orgs, f"no orgs: {st} {orgs}"
    ORG_ID = orgs[0]["id"]
    print(f"auth ok — org {ORG_ID}")


def create_agent(name):
    body = {"name": name, "type": "sqlite", "config": {"database": CHINOOK}, "credentials": {}}
    st, data = req("POST", "/api/data_sources", body)
    if st in (200, 201) and data and data.get("id"):
        print(f"  agent '{name}' -> {data['id']}")
        return data["id"]
    print(f"  ! agent '{name}' failed: {st} {str(data)[:200]}")
    return None


def create_instruction(text, title, category, load_mode, status, source_type, ds_ids):
    body = {
        "text": text, "title": title, "category": category, "load_mode": load_mode,
        "status": status, "source_type": source_type, "data_source_ids": ds_ids or [],
    }
    st, data = req("POST", "/api/instructions/global", body)
    if st in (200, 201) and data and data.get("id"):
        return data["id"]
    print(f"  ! instr '{title}' failed: {st} {str(data)[:200]}")
    return None


def main():
    if not wait_for_server():
        print("server never came up"); sys.exit(1)
    setup_auth()

    print("creating agents…")
    agent_names = ["Revenue Analyst", "Growth", "Marketing", "Product Insights", "Finance Ops"]
    agents = {}
    for n in agent_names:
        aid = create_agent(n)
        if aid:
            agents[n] = aid
    A = lambda *names: [agents[n] for n in names if n in agents]

    print("creating instructions…")
    # (text, title, category, load_mode, status, source_type, ds_ids)
    rows = [
        # --- Global (all agents) ---
        ("Revenue is always **net of refunds** and excludes internal test accounts (emails ending in @bow.dev).",
         "Net revenue definition", "data_modeling", "always", "published", "user", []),
        ("Always format currency as USD with thousands separators and no decimals for totals over $1,000.",
         "Currency formatting", "visualizations", "always", "published", "user", []),
        ("When the user says \"last quarter\" interpret it as the most recent **completed** fiscal quarter, not the current one.",
         "Fiscal quarter semantics", "general", "intelligent", "published", "user", []),
        ("Never run UPDATE, DELETE, INSERT or DDL. This is a read-only analytics environment.",
         "Read-only guardrail", "system", "always", "published", "user", []),
        ("Prefer bar charts for categorical comparisons and line charts for time series. Avoid pie charts beyond 5 slices.",
         "Charting conventions", "visualizations", "intelligent", "published", "ai", []),
        ("\"Active customer\" = a customer with at least one purchase in the trailing 30 days.",
         "Active customer term", "data_modeling", "always", "draft", "user", []),

        # --- Revenue Analyst ---
        ("To compute monthly recurring revenue, sum invoice totals grouped by billing month from the `invoices` table, joining `customers` for segment.",
         "MRR computation skill", "data_modeling", "intelligent", "published", "user", A("Revenue Analyst")),
        ("Pipeline coverage = open pipeline / quota for the period. Flag any segment below 3x coverage.",
         "Pipeline coverage", "general", "intelligent", "published", "user", A("Revenue Analyst")),
        ("Churned revenue should be attributed to the month the cancellation takes effect, not when it was requested.",
         "Churn attribution", "data_modeling", "always", "published", "git", A("Revenue Analyst")),
        ("Deprecated: legacy ARR bridge logic. Do not use for new analyses.",
         "Legacy ARR bridge", "data_modeling", "disabled", "draft", "git", A("Revenue Analyst")),

        # --- Growth ---
        ("Cohort users by their first-purchase month; report % retained in each subsequent month for 12 months.",
         "Cohort retention skill", "data_modeling", "intelligent", "published", "user", A("Growth")),
        ("Activation = a new user reaching 3 key events within their first 7 days.",
         "Activation definition", "general", "always", "published", "ai", A("Growth")),
        ("When analyzing funnels, always show absolute counts AND step-to-step conversion %.",
         "Funnel display rule", "visualizations", "intelligent", "published", "user", A("Growth")),

        # --- Marketing ---
        ("Attribute conversions using last-non-direct-touch within a 30-day lookback window.",
         "Attribution model", "data_modeling", "always", "published", "user", A("Marketing")),
        ("Group campaigns by UTM source/medium; treat null UTM as 'organic'.",
         "Campaign grouping", "data_modeling", "intelligent", "published", "git", A("Marketing")),
        ("CAC = total blended spend / new customers acquired in the same period.",
         "CAC definition", "general", "always", "draft", "user", A("Marketing")),

        # --- Product Insights ---
        ("Feature adoption = distinct users who used the feature / distinct active users, over a 28-day window.",
         "Feature adoption skill", "data_modeling", "intelligent", "published", "user", A("Product Insights")),
        ("When asked about 'engagement', default to WAU/MAU stickiness unless the user specifies otherwise.",
         "Engagement default", "general", "intelligent", "published", "ai", A("Product Insights")),
        ("Always annotate release dates as vertical markers on product time-series charts.",
         "Release annotations", "visualizations", "always", "published", "user", A("Product Insights")),

        # --- Finance Ops ---
        ("Use accrual accounting: recognize revenue when earned, not when cash is received.",
         "Accrual basis", "general", "always", "published", "git", A("Finance Ops")),
        ("Gross margin = (revenue - COGS) / revenue. COGS lives in the `cost_of_goods` ledger.",
         "Gross margin skill", "data_modeling", "intelligent", "published", "user", A("Finance Ops")),
        ("Round all financial ratios to one decimal place and show as percentages.",
         "Ratio rounding", "visualizations", "disabled", "draft", "user", A("Finance Ops")),

        # --- Multi-agent reach ---
        ("Fiscal year starts February 1. All year-over-year comparisons must align to fiscal periods.",
         "Fiscal calendar", "data_modeling", "always", "published", "git", A("Revenue Analyst", "Finance Ops", "Marketing")),
        ("EMEA includes the UK; APAC includes India. Use the `region_map` reference for country rollups.",
         "Region rollups", "data_modeling", "intelligent", "published", "user", A("Revenue Analyst", "Growth")),
        ("Exclude weekends from daily-average calculations for B2B metrics.",
         "Business-day averaging", "general", "intelligent", "draft", "ai", A("Growth", "Product Insights")),
    ]
    n_ok = 0
    for r in rows:
        if create_instruction(*r):
            n_ok += 1
    print(f"done — {len(agents)} agents, {n_ok}/{len(rows)} instructions")


if __name__ == "__main__":
    main()
