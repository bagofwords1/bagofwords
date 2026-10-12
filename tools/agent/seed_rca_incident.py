#!/usr/bin/env python3
"""Generate the "Checkout Ops" demo database: a planted checkout incident for
root-cause-analysis (RCA) evaluation of sub-runs.

Deterministic (seeded RNG) so every loop sees the same numbers. Writes a
SQLite file (default ``backend/demo-datasources/rca_incident.sqlite``) with
daily-grain data for 2026-08-01 .. 2026-09-30:

  orders                  date, region, platform, app_version, payment_provider,
                          status (paid|failed), amount
  sessions                date, region, platform, app_version, sessions
  deploys                 date, service, version, notes
  payment_provider_status provider, region, date, incident (0|1)
  price_changes           date, sku_group, old_price, new_price
  campaigns               start, end, region, channel, spend

Planted truth: from 2026-09-14 the checkout failure rate jumps from ~3% to
~60% ONLY for platform=ios AND app_version=5.2.0 AND payment_provider=PayFlow;
``deploys`` shows checkout-service 5.2.0 shipped on 2026-09-14.

Decoys (each looks causal on its own, none explains the drop):
  * price change on 2026-09-10 (sku_group=premium) — neither series moves;
  * APAC campaign 2026-09-12..09-20 — sessions +40% in apac, paid orders flat (low-intent traffic);
  * PayFlow incident flag 2026-09-15 in us-east only — a 6h blip that recovers;
  * ordinary weekly seasonality (weekends lower).

Usage (from backend/ so the venv is active):
    uv run python ../tools/agent/seed_rca_incident.py [--out PATH] [--install]

``--install`` registers the file as the "Checkout Ops" demo source through
the running backend's API (same flow as seed_org.py --demo), using
BOW_BASE_URL / BOW_ADMIN_EMAIL / BOW_ADMIN_PASSWORD.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import random
import sqlite3
import sys

START = dt.date(2026, 8, 1)
END = dt.date(2026, 9, 30)
INCIDENT_DAY = dt.date(2026, 9, 14)

REGIONS = ["us-east", "us-west", "emea", "apac"]
PLATFORMS = ["ios", "android", "web"]
APP_VERSIONS = {"ios": ["5.1.0", "5.2.0"], "android": ["5.1.0", "5.2.0"], "web": ["web"]}
PROVIDERS = ["PayFlow", "Stripe", "Adyen"]

BASE_FAIL = 0.03
INCIDENT_FAIL = 0.60
BASE_SESSIONS = {"us-east": 2600, "us-west": 1800, "emea": 2200, "apac": 1400}
PLATFORM_SHARE = {"ios": 0.50, "android": 0.30, "web": 0.20}
PROVIDER_SHARE = {"PayFlow": 0.6, "Stripe": 0.25, "Adyen": 0.15}
CONVERSION = 0.11  # sessions -> checkout attempts


def _daterange():
    d = START
    while d <= END:
        yield d
        d += dt.timedelta(days=1)


def _version_share(platform: str, day: dt.date) -> dict:
    """iOS/Android roll 5.2.0 out gradually from 09-14; before that only 5.1.0."""
    if platform == "web":
        return {"web": 1.0}
    if day < INCIDENT_DAY:
        return {"5.1.0": 1.0, "5.2.0": 0.0}
    days_in = (day - INCIDENT_DAY).days
    share_new = min(0.9, 0.7 + 0.04 * days_in)
    return {"5.1.0": 1.0 - share_new, "5.2.0": share_new}


def _is_planted(platform: str, version: str, provider: str, day: dt.date) -> bool:
    return day >= INCIDENT_DAY and platform == "ios" and version == "5.2.0" and provider == "PayFlow"


def _is_blip(provider: str, region: str, day: dt.date) -> bool:
    # decoy: a six-hour PayFlow incident in us-east on 09-15 (quarter of the day)
    return provider == "PayFlow" and region == "us-east" and day == dt.date(2026, 9, 15)


def build(path: pathlib.Path, seed: int = 20260914) -> dict:
    rng = random.Random(seed)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,
            region TEXT NOT NULL,
            platform TEXT NOT NULL,
            app_version TEXT NOT NULL,
            payment_provider TEXT NOT NULL,
            status TEXT NOT NULL,
            amount REAL NOT NULL
        );
        CREATE TABLE sessions (
            date TEXT NOT NULL,
            region TEXT NOT NULL,
            platform TEXT NOT NULL,
            app_version TEXT NOT NULL,
            sessions INTEGER NOT NULL
        );
        CREATE TABLE deploys (
            date TEXT NOT NULL,
            service TEXT NOT NULL,
            version TEXT NOT NULL,
            notes TEXT
        );
        CREATE TABLE payment_provider_status (
            provider TEXT NOT NULL,
            region TEXT NOT NULL,
            date TEXT NOT NULL,
            incident INTEGER NOT NULL
        );
        CREATE TABLE price_changes (
            date TEXT NOT NULL,
            sku_group TEXT NOT NULL,
            old_price REAL NOT NULL,
            new_price REAL NOT NULL
        );
        CREATE TABLE campaigns (
            start TEXT NOT NULL,
            end TEXT NOT NULL,
            region TEXT NOT NULL,
            channel TEXT NOT NULL,
            spend REAL NOT NULL
        );
        """
    )

    orders_rows = []
    sessions_rows = []
    stats = {"paid_before": 0, "paid_after": 0, "planted_fail": 0, "planted_total": 0}
    for day in _daterange():
        weekend = day.weekday() >= 5
        season = 0.78 if weekend else 1.0
        campaign = 1.4 if (day >= dt.date(2026, 9, 12) and day <= dt.date(2026, 9, 20)) else 1.0
        for region in REGIONS:
            region_sessions = BASE_SESSIONS[region] * season * (campaign if region == "apac" else 1.0)
            for platform in PLATFORMS:
                for version, vshare in _version_share(platform, day).items():
                    n_sessions = int(region_sessions * PLATFORM_SHARE[platform] * vshare * rng.uniform(0.93, 1.07))
                    if n_sessions <= 0:
                        continue
                    sessions_rows.append((day.isoformat(), region, platform, version, n_sessions))
                    # The APAC campaign brings low-intent traffic: sessions rise,
                    # checkout attempts do not (the decoy must not move the metric).
                    intent = (1.0 / campaign) if (region == "apac" and campaign > 1.0) else 1.0
                    attempts = int(n_sessions * CONVERSION * intent * rng.uniform(0.95, 1.05))
                    for _ in range(attempts):
                        provider = rng.choices(PROVIDERS, weights=[PROVIDER_SHARE[p] for p in PROVIDERS])[0]
                        fail_p = BASE_FAIL
                        planted = _is_planted(platform, version, provider, day)
                        if planted:
                            fail_p = INCIDENT_FAIL
                        if _is_blip(provider, region, day):
                            fail_p = max(fail_p, 0.03 + 0.25 * 0.25)  # 6h at 28% ≈ +6pp for the day
                        failed = rng.random() < fail_p
                        amount = round(rng.lognormvariate(3.6, 0.45), 2)
                        orders_rows.append((day.isoformat(), region, platform, version, provider,
                                            "failed" if failed else "paid", amount))
                        if planted:
                            stats["planted_total"] += 1
                            stats["planted_fail"] += int(failed)
                        if not failed:
                            if day < INCIDENT_DAY:
                                stats["paid_before"] += 1
                            else:
                                stats["paid_after"] += 1

    cur.executemany("INSERT INTO orders(date,region,platform,app_version,payment_provider,status,amount) VALUES (?,?,?,?,?,?,?)", orders_rows)
    cur.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", sessions_rows)
    cur.executemany("INSERT INTO deploys VALUES (?,?,?,?)", [
        ("2026-08-19", "catalog-service", "3.4.1", "search ranking tweaks"),
        ("2026-09-02", "checkout-service", "5.1.3", "retry tuning for Adyen"),
        ("2026-09-14", "checkout-service", "5.2.0", "new payment sheet; PayFlow SDK upgrade on iOS"),
        ("2026-09-22", "web-frontend", "2.88.0", "footer redesign"),
    ])
    status_rows = []
    for day in _daterange():
        for provider in PROVIDERS:
            for region in REGIONS:
                status_rows.append((provider, region, day.isoformat(), 1 if _is_blip(provider, region, day) else 0))
    cur.executemany("INSERT INTO payment_provider_status VALUES (?,?,?,?)", status_rows)
    cur.executemany("INSERT INTO price_changes VALUES (?,?,?,?)", [
        ("2026-09-10", "premium", 49.0, 54.0),
    ])
    cur.executemany("INSERT INTO campaigns VALUES (?,?,?,?,?)", [
        ("2026-08-05", "2026-08-09", "us-west", "search", 12000.0),
        ("2026-09-12", "2026-09-20", "apac", "social", 38000.0),
    ])
    con.commit()
    con.close()
    stats["orders"] = len(orders_rows)
    stats["sessions_rows"] = len(sessions_rows)
    return stats


def install(path: pathlib.Path) -> dict:
    """Register the file as the Checkout Ops demo source via the API."""
    import httpx

    base = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
    email = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
    password = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
    c = httpx.Client(base_url=base, timeout=120)
    tok = c.post("/api/auth/jwt/login", data={"username": email, "password": password}).json()["access_token"]
    orgs = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()
    org = orgs[0]["id"]
    h = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
    demos = c.get("/api/data_sources/demos", headers=h).json()
    demo = next((d for d in demos if d["id"] == "checkout_ops"), None)
    if not demo:
        return {"error": "checkout_ops demo is not registered in the backend"}
    r = c.post("/api/data_sources/demos/checkout_ops", headers=h)
    return r.json() if r.status_code < 300 else {"error": r.status_code, "body": r.text[:300]}


def main() -> int:
    p = argparse.ArgumentParser()
    default_out = pathlib.Path(__file__).resolve().parents[2] / "backend" / "demo-datasources" / "rca_incident.sqlite"
    p.add_argument("--out", default=str(default_out))
    p.add_argument("--install", action="store_true")
    args = p.parse_args()
    out = pathlib.Path(args.out)
    stats = build(out)
    print({"path": str(out), **stats})
    if args.install:
        print(install(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
