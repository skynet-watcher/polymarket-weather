"""
backfill_model_forecasts.py
===========================
Backfill historical model forecasts from Open-Meteo Previous Runs.

This fills model_forecasts with fixed-lead historical predictions so Tests 2, 3,
4, 5, and 9 can be reproduced from repo data instead of one-off notebooks.

Important distinction:
  - Previous Runs answers: "What did this model predict N days before the valid day?"
  - It does NOT prove the forecast was available at Polymarket market open.
  - Market-open tests still need market_start_utc/first_seen_utc plus live order books.

For each station/model/date/lead, this script requests hourly
temperature_2m_previous_dayN and stores the station-local daily high in model_forecasts.

Usage:
    python scripts/backfill_model_forecasts.py --start 2026-05-01 --end 2026-05-31
    python scripts/backfill_model_forecasts.py --days 30 --cities Madrid Ankara Singapore
    python scripts/backfill_model_forecasts.py --from-markets
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill_models")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "weather.db")
STATIONS_F = os.path.join(REPO_ROOT, "data", "city_stations.json")
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

MODELS = [
    "gfs_seamless",
    "ecmwf_ifs025",
    "icon_seamless",
    "gem_seamless",
    "meteofrance_seamless",
]
DEFAULT_LEADS = [1, 2, 3]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_cities() -> list[dict]:
    with open(STATIONS_F) as f:
        return json.load(f)["cities"]


def _date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def _range_from_markets(conn: sqlite3.Connection) -> tuple[dt.date, dt.date]:
    row = conn.execute("""
        SELECT MIN(settlement_date), MAX(settlement_date)
        FROM weather_markets
        WHERE settlement_date IS NOT NULL
          AND settlement_date < date('now')
    """).fetchone()
    if not row or not row[0] or not row[1]:
        raise SystemExit("No historical weather_markets found; pass --start/--end or run backfill_history.py first")
    return dt.date.fromisoformat(row[0]), dt.date.fromisoformat(row[1])


def _estimated_run_utc(valid_date: str, lead_days: int) -> str:
    """Previous Runs exposes fixed-day lead values, not exact run metadata.

    Use 00 UTC lead-day initialization as a stable surrogate so the existing
    model_forecasts unique key can represent one row per station/model/lead/date.
    """
    date = dt.date.fromisoformat(valid_date) - dt.timedelta(days=lead_days)
    return dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc).isoformat()


async def _fetch_city_model(
    client: httpx.AsyncClient,
    city: dict,
    model: str,
    start: dt.date,
    end: dt.date,
    leads: list[int],
) -> dict[str, dict[int, tuple[float, float]]]:
    """Return {YYYY-MM-DD: {lead_days: (high_c, low_c)}} for one city/model."""
    hourly_vars = []
    for lead in leads:
        hourly_vars.append(f"temperature_2m_previous_day{lead}")

    r = await client.get(
        PREVIOUS_RUNS_URL,
        params={
            "latitude": city["lat"],
            "longitude": city["lon"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": ",".join(hourly_vars),
            "models": model,
            "timezone": city["timezone"],
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data.get("reason", "Open-Meteo Previous Runs error"))

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    by_day: dict[str, dict[int, list[float]]] = {}
    for idx, stamp in enumerate(times):
        day = stamp[:10]
        by_day.setdefault(day, {lead: [] for lead in leads})
        for lead in leads:
            values = hourly.get(f"temperature_2m_previous_day{lead}") or []
            if idx < len(values) and values[idx] is not None:
                by_day[day][lead].append(float(values[idx]))

    result: dict[str, dict[int, tuple[float, float]]] = {}
    for day, lead_values in by_day.items():
        result[day] = {}
        for lead, temps in lead_values.items():
            if temps:
                result[day][lead] = (max(temps), min(temps))
    return result


def _store_forecasts(
    conn: sqlite3.Connection,
    city: dict,
    model: str,
    forecasts: dict[str, dict[int, tuple[float, float]]],
    fetched_utc: str,
    replace: bool,
) -> int:
    sql_verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    saved = 0
    for valid_date, lead_values in forecasts.items():
        for lead, (high_c, low_c) in lead_values.items():
            model_run_utc = _estimated_run_utc(valid_date, lead)
            metadata = {
                "source": "open_meteo_previous_runs",
                "lead_days": lead,
                "hourly_variable": f"temperature_2m_previous_day{lead}",
                "timezone": city["timezone"],
                "model_run_utc_note": "estimated surrogate: valid_date - lead_days at 00:00 UTC",
            }
            raw = {
                "source": "open_meteo_previous_runs",
                "city": city["city"],
                "station": city["station"],
                "model": model,
                "forecast_date": valid_date,
                "lead_days": lead,
                "high_c": high_c,
                "low_c": low_c,
            }
            before = conn.total_changes
            conn.execute(f"""
                {sql_verb} INTO model_forecasts
                    (station, city, model, model_run_utc, fetched_utc,
                     forecast_date, horizon_hours, high_c, low_c, lat, lon,
                     model_run_is_estimated, source_metadata_json, raw_payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                city["station"], city["city"], model, model_run_utc, fetched_utc,
                valid_date, lead * 24, high_c, low_c, city["lat"], city["lon"],
                1, json.dumps(metadata, sort_keys=True), json.dumps(raw, sort_keys=True),
            ))
            if conn.total_changes > before:
                saved += 1
    conn.commit()
    return saved


async def backfill(
    conn: sqlite3.Connection,
    start: dt.date,
    end: dt.date,
    city_filter: list[str] | None,
    models: list[str],
    leads: list[int],
    replace: bool = False,
) -> int:
    cities = _load_cities()
    if city_filter:
        requested = {c.lower() for c in city_filter}
        cities = [c for c in cities if c["city"].lower() in requested or c["slug"].lower() in requested]
    if not cities:
        raise SystemExit("No matching cities")

    fetched_utc = _now()
    total_saved = 0
    total = len(cities) * len(models)
    done = 0
    async with httpx.AsyncClient(headers={"User-Agent": "polymarket-weather/0.1 research"}) as client:
        for city in cities:
            for model in models:
                done += 1
                log.info("[%03d/%03d] %-12s %s", done, total, city["city"], model)
                try:
                    forecasts = await _fetch_city_model(client, city, model, start, end, leads)
                    saved = _store_forecasts(conn, city, model, forecasts, fetched_utc, replace)
                    total_saved += saved
                    log.info("  saved=%d dates=%d", saved, len(forecasts))
                except Exception as exc:
                    log.warning("  failed: %s", exc)
                await asyncio.sleep(0.15)
    return total_saved


def _summary(conn: sqlite3.Connection) -> None:
    rows = conn.execute("""
        SELECT model, MIN(forecast_date), MAX(forecast_date), COUNT(*)
        FROM model_forecasts
        WHERE source_metadata_json LIKE '%open_meteo_previous_runs%'
        GROUP BY model
        ORDER BY model
    """).fetchall()
    print("\n── Historical model forecast summary ──")
    for model, min_date, max_date, count in rows:
        print(f"  {model:22s} {count:6d} rows  {min_date} → {max_date}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill historical model forecasts")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--days", type=int, help="Past N days ending yesterday")
    parser.add_argument("--from-markets", action="store_true",
                        help="Use min/max historical settlement_date from weather_markets")
    parser.add_argument("--cities", nargs="+", help="Subset of city names or slugs")
    parser.add_argument("--models", nargs="+", default=MODELS, choices=MODELS)
    parser.add_argument("--leads", nargs="+", type=int, default=DEFAULT_LEADS,
                        help="Previous-run lead days to fetch, e.g. 1 2 3")
    parser.add_argument("--replace", action="store_true",
                        help="Replace existing rows with the same station/model/run/date")
    parser.add_argument("--db", default=DB_PATH, help="SQLite DB path")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    today = dt.datetime.now(dt.timezone.utc).date()
    if args.from_markets:
        start_date, end_date = _range_from_markets(conn)
    elif args.start:
        start_date = dt.date.fromisoformat(args.start)
        end_date = dt.date.fromisoformat(args.end) if args.end else start_date
    else:
        days = args.days or 30
        end_date = today - dt.timedelta(days=1)
        start_date = end_date - dt.timedelta(days=days - 1)

    log.info("Backfilling model forecasts %s → %s | leads=%s | models=%s",
             start_date, end_date, args.leads, args.models)
    saved_count = asyncio.run(backfill(
        conn, start_date, end_date, args.cities, args.models, args.leads, args.replace
    ))
    log.info("Backfill complete: saved %d rows", saved_count)
    _summary(conn)
    conn.close()
