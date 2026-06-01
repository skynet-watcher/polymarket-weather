"""
discover_markets.py
===================
Scrapes Polymarket weather events to find all active temperature markets,
extracts condition IDs and YES/NO token IDs via the CLOB API,
and upserts into weather.db.

Run daily (or on-demand) before the trading window opens.

Usage:
    cd /Users/eric/polymarket-weather
    .venv/bin/python scripts/discover_markets.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import sqlite3

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("discover")

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH     = os.path.join(REPO_ROOT, "weather.db")
STATIONS_F  = os.path.join(REPO_ROOT, "data", "city_stations.json")
CLOB_BASE   = "https://clob.polymarket.com"
PM_BASE     = "https://polymarket.com"

# ── DB setup ──────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS weather_markets (
        condition_id    TEXT PRIMARY KEY,
        city            TEXT NOT NULL,
        station         TEXT NOT NULL,
        settlement_date TEXT NOT NULL,
        bucket_type     TEXT NOT NULL,
        temp_c          REAL,
        yes_token_id    TEXT,
        no_token_id     TEXT,
        question        TEXT,
        active          INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS ob_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        condition_id    TEXT NOT NULL,
        ts_utc          TEXT NOT NULL,
        yes_bid         REAL,
        yes_ask         REAL,
        no_bid          REAL,
        no_ask          REAL,
        yes_mid         REAL
    );
    CREATE INDEX IF NOT EXISTS ix_ob_cid_ts ON ob_snapshots(condition_id, ts_utc);
    CREATE TABLE IF NOT EXISTS wx_observations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        station         TEXT NOT NULL,
        city            TEXT NOT NULL,
        ts_utc          TEXT NOT NULL,
        temp_c          REAL,
        daily_high_c    REAL,
        source          TEXT DEFAULT 'open-meteo'
    );
    CREATE INDEX IF NOT EXISTS ix_wx_station_ts ON wx_observations(station, ts_utc);
    CREATE TABLE IF NOT EXISTS wx_forecasts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        city            TEXT NOT NULL,
        forecast_date   TEXT NOT NULL,
        fetched_at_utc  TEXT NOT NULL,
        high_c          REAL,
        low_c           REAL,
        source          TEXT DEFAULT 'open-meteo'
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc          TEXT NOT NULL,
        city            TEXT NOT NULL,
        alert_type      TEXT NOT NULL,
        detail_json     TEXT
    );
    """)
    conn.commit()


# ── Market discovery ──────────────────────────────────────────────────────────

def _load_stations() -> dict[str, dict]:
    with open(STATIONS_F) as f:
        data = json.load(f)
    return {c["slug"]: c for c in data["cities"]}


def _parse_temp_and_type(question: str) -> tuple[str, float | None]:
    """Return (bucket_type, temp_c) from a question string."""
    q = question.lower()
    m = re.search(r"be (\d+)°c or below", q)
    if m:
        return "below_eq", float(m.group(1))
    m = re.search(r"be (\d+)°c or higher", q)
    if m:
        return "above_eq", float(m.group(1))
    m = re.search(r"be (\d+)°c on", q)
    if m:
        return "exact", float(m.group(1))
    return "unknown", None


async def _fetch_event_markets(client: httpx.AsyncClient, slug: str) -> list[dict]:
    """Fetch all markets for a city event from the Polymarket website."""
    url = f"{PM_BASE}/event/highest-temperature-in-{slug}-on-"
    # We need today and tomorrow's slugs
    today = dt.date.today()
    dates_to_try = [
        today.strftime("%B-%-d-%Y").lower(),
        (today + dt.timedelta(days=1)).strftime("%B-%-d-%Y").lower(),
    ]

    markets = []
    for date_slug in dates_to_try:
        full_url = f"{url}{date_slug}"
        try:
            r = await client.get(full_url, timeout=10, follow_redirects=True)
            if r.status_code != 200:
                continue
            html = r.text

            # Extract condition IDs
            cids = list(dict.fromkeys(re.findall(r"0x[a-f0-9]{62,64}", html)))
            questions = re.findall(r'"question":"([^"]+temperature[^"]+)"', html)

            if cids and questions:
                settlement_date = (today if date_slug == dates_to_try[0]
                                   else today + dt.timedelta(days=1)).isoformat()
                for i, cid in enumerate(cids[:len(questions)]):
                    q = questions[i] if i < len(questions) else ""
                    btype, temp = _parse_temp_and_type(q)
                    markets.append({
                        "condition_id": cid,
                        "question": q,
                        "settlement_date": settlement_date,
                        "bucket_type": btype,
                        "temp_c": temp,
                    })
                log.info("  %s %s: %d markets", slug, date_slug, len(cids))
        except Exception as e:
            log.warning("  %s %s: %s", slug, date_slug, e)

    return markets


async def _fetch_tokens(client: httpx.AsyncClient, condition_id: str) -> tuple[str | None, str | None]:
    """Get YES/NO token IDs from CLOB."""
    try:
        r = await client.get(f"{CLOB_BASE}/markets/{condition_id}", timeout=5)
        if r.status_code != 200:
            return None, None
        data = r.json()
        tokens = data.get("tokens", [])
        yes = next((t["token_id"] for t in tokens if t.get("outcome") == "Yes"), None)
        no  = next((t["token_id"] for t in tokens if t.get("outcome") == "No"), None)
        return yes, no
    except Exception:
        return None, None


async def discover(conn: sqlite3.Connection) -> int:
    stations = _load_stations()
    upserted = 0

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        for slug, station_info in stations.items():
            markets = await _fetch_event_markets(client, slug)
            for m in markets:
                yes_tok, no_tok = await _fetch_tokens(client, m["condition_id"])
                await asyncio.sleep(0.1)

                conn.execute("""
                    INSERT INTO weather_markets
                        (condition_id, city, station, settlement_date, bucket_type,
                         temp_c, yes_token_id, no_token_id, question, active)
                    VALUES (?,?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(condition_id) DO UPDATE SET
                        yes_token_id=excluded.yes_token_id,
                        no_token_id=excluded.no_token_id,
                        active=1
                """, (
                    m["condition_id"],
                    station_info["city"],
                    station_info["station"],
                    m["settlement_date"],
                    m["bucket_type"],
                    m["temp_c"],
                    yes_tok, no_tok,
                    m["question"],
                ))
                upserted += 1
            conn.commit()
            await asyncio.sleep(0.3)

    return upserted


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    n = asyncio.run(discover(conn))
    log.info("Upserted %d markets into %s", n, DB_PATH)
    conn.close()
