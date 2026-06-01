"""
fetch_weather.py
================
Fetches current METAR observations from aviationweather.gov for each
settlement station — the same underlying data Weather Underground uses
to settle Polymarket temperature markets.

Also fetches open-meteo daily forecast for comparison (labelled separately).

Tracks running daily high per station by storing each observation and
computing max(temp_c) since local midnight.

Poll interval: every 30 minutes (METAR updates ~hourly at most stations).

Usage:
    cd /Users/eric/polymarket-weather
    .venv/bin/python scripts/fetch_weather.py [--once] [--interval 1800]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("wx_fetch")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH   = os.path.join(REPO_ROOT, "weather.db")

# ICAO station → city + local timezone (for computing "local day" highs)
STATIONS: dict[str, dict] = {
    "RKSI": {"city": "Seoul",      "tz": "Asia/Seoul"},
    "ZBAA": {"city": "Beijing",    "tz": "Asia/Shanghai"},   # Beijing & HK anomaly
    "EGLC": {"city": "London",     "tz": "Europe/London"},
    "RJTT": {"city": "Tokyo",      "tz": "Asia/Tokyo"},
    "KLGA": {"city": "NYC",        "tz": "America/New_York"},
    "LFPB": {"city": "Paris",      "tz": "Europe/Paris"},
    "KMIA": {"city": "Miami",      "tz": "America/New_York"},
    "WSSS": {"city": "Singapore",  "tz": "Asia/Singapore"},
    "LEMD": {"city": "Madrid",     "tz": "Europe/Madrid"},
    "EFHK": {"city": "Moscow",     "tz": "Europe/Helsinki"},  # Moscow anomaly
    "EDDM": {"city": "Munich",     "tz": "Europe/Berlin"},
    "EHAM": {"city": "Amsterdam",  "tz": "Europe/Amsterdam"},
    "LTAC": {"city": "Ankara",     "tz": "Europe/Istanbul"},
    "NZWN": {"city": "Wellington", "tz": "Pacific/Auckland"},
    "ZGSZ": {"city": "Shenzhen",   "tz": "Asia/Shanghai"},
    "ZGGG": {"city": "Guangzhou",  "tz": "Asia/Shanghai"},
}

# Hong Kong uses ZBAA (Beijing) per Polymarket resolution — flagged as anomaly
# We fetch ZBAA once and assign to both Beijing and Hong Kong
HK_ANOMALY_STATION = "ZBAA"

AVWX_BASE    = "https://aviationweather.gov/api/data/metar"
OPEN_METEO   = "https://api.open-meteo.com/v1/forecast"

# open-meteo coords for forecast (labelled 'forecast' not 'metar')
FORECAST_COORDS: dict[str, tuple] = {
    "Seoul":      (37.46, 126.44, "Asia/Seoul"),
    "Hong Kong":  (22.31, 113.92, "Asia/Shanghai"),
    "London":     (51.51,  0.06,  "Europe/London"),
    "Tokyo":      (35.55, 139.78, "Asia/Tokyo"),
    "NYC":        (40.78, -73.87, "America/New_York"),
    "Paris":      (48.97,  2.44,  "Europe/Paris"),
    "Beijing":    (40.08, 116.60, "Asia/Shanghai"),
    "Miami":      (25.80, -80.29, "America/New_York"),
    "Singapore":  ( 1.36, 103.99, "Asia/Singapore"),
    "Madrid":     (40.47,  -3.56, "Europe/Madrid"),
    "Moscow":     (55.75,  37.62, "Europe/Moscow"),   # actual Moscow coords for forecast
    "Munich":     (48.35,  11.79, "Europe/Berlin"),
    "Amsterdam":  (52.31,   4.76, "Europe/Amsterdam"),
    "Ankara":     (39.93,  32.86, "Europe/Istanbul"),
    "Wellington": (-41.33, 174.81,"Pacific/Auckland"),
    "Shenzhen":   (22.64, 113.81, "Asia/Shanghai"),
    "Guangzhou":  (23.39, 113.30, "Asia/Shanghai"),
}


# ── METAR fetch ───────────────────────────────────────────────────────────────

async def fetch_metars(client: httpx.AsyncClient) -> dict[str, dict]:
    """Fetch latest METAR for all settlement stations. Returns {station: obs}."""
    all_stations = ",".join(STATIONS.keys())
    try:
        r = await client.get(AVWX_BASE, params={
            "ids":    all_stations,
            "format": "json",
            "hours":  2,
        }, timeout=15)
        if r.status_code != 200:
            log.warning("METAR fetch failed: %s", r.status_code)
            return {}
        data = r.json()
        # aviationweather returns list sorted newest-first per station
        result: dict[str, dict] = {}
        seen: set[str] = set()
        for obs in data:
            sid = obs.get("stationId") or obs.get("icaoId", "")
            if sid and sid not in seen:
                seen.add(sid)
                result[sid] = obs
        return result
    except Exception as e:
        log.warning("METAR fetch error: %s", e)
        return {}


def _running_daily_high(conn: sqlite3.Connection, station: str) -> float | None:
    """Max observed temp since local midnight today for this station."""
    today = dt.date.today().isoformat()
    row = conn.execute("""
        SELECT MAX(temp_c) FROM wx_observations
        WHERE station=? AND source='metar' AND DATE(ts_utc)=?
    """, (station, today)).fetchone()
    return row[0] if row else None


# ── Forecast fetch ────────────────────────────────────────────────────────────

async def fetch_forecasts(client: httpx.AsyncClient, conn: sqlite3.Connection) -> None:
    """Fetch open-meteo daily forecast for each city (labelled as forecast, not settlement source)."""
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    today = dt.date.today().isoformat()
    for city, (lat, lon, tz) in FORECAST_COORDS.items():
        try:
            r = await client.get(OPEN_METEO, params={
                "latitude": lat, "longitude": lon, "timezone": tz,
                "daily": "temperature_2m_max,temperature_2m_min",
                "forecast_days": 2, "temperature_unit": "celsius",
            }, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            daily = data.get("daily", {})
            high  = (daily.get("temperature_2m_max") or [None])[0]
            low   = (daily.get("temperature_2m_min") or [None])[0]
            conn.execute("""
                INSERT INTO wx_forecasts (city, forecast_date, fetched_at_utc, high_c, low_c, source)
                VALUES (?,?,?,?,?,'open-meteo')
            """, (city, today, ts, high, low))
        except Exception as e:
            log.warning("Forecast %s: %s", city, e)
    conn.commit()


# ── Main fetch loop ───────────────────────────────────────────────────────────

async def fetch_all(conn: sqlite3.Connection) -> int:
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    saved = 0

    async with httpx.AsyncClient() as client:
        metars = await fetch_metars(client)
        await fetch_forecasts(client, conn)

    for station, info in STATIONS.items():
        obs = metars.get(station)
        if not obs:
            log.warning("  %-6s  no METAR", station)
            continue

        temp_c = obs.get("temp")
        if temp_c is None:
            continue

        # Insert observation
        conn.execute("""
            INSERT INTO wx_observations (station, city, ts_utc, temp_c, daily_high_c, source)
            VALUES (?,?,?,?,?,?)
        """, (station, info["city"], ts, temp_c, None, "metar"))
        conn.commit()

        # Compute running high now that this row is inserted
        running_high = _running_daily_high(conn, station)

        # Update daily_high_c on this row
        conn.execute("""
            UPDATE wx_observations SET daily_high_c=?
            WHERE station=? AND ts_utc=? AND source='metar'
        """, (running_high, station, ts))
        conn.commit()

        obs_time = str(obs.get("reportTime", obs.get("obsTime", "")))[:16]
        log.info("  %-6s %-12s  temp=%5.1f°C  day_high=%5.1f°C  metar_time=%s",
                 station, info["city"], temp_c,
                 running_high or 0, obs_time)
        saved += 1

        # Hong Kong anomaly: also log ZBAA as Hong Kong
        if station == HK_ANOMALY_STATION:
            conn.execute("""
                INSERT INTO wx_observations (station, city, ts_utc, temp_c, daily_high_c, source)
                VALUES (?,?,?,?,?,?)
            """, (station, "Hong Kong", ts, temp_c, running_high, "metar"))
            conn.commit()

    return saved


async def loop(interval_s: int = 1800) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    log.info("Starting METAR fetcher (resolution-source accurate) | interval=%ds", interval_s)
    while True:
        try:
            n = await fetch_all(conn)
            log.info("Fetched %d station readings", n)
        except Exception as e:
            log.exception("fetch error: %s", e)
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",     action="store_true")
    parser.add_argument("--interval", type=int, default=1800)
    args = parser.parse_args()

    if args.once:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        n = asyncio.run(fetch_all(conn))
        print(f"Fetched {n} station readings")
        conn.close()
    else:
        asyncio.run(loop(args.interval))
