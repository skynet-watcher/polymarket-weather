"""
fetch_weather.py
================
Fetches current observed temperature and daily high from open-meteo
for each settlement station city, every 15 minutes.

Uses open-meteo (free, no API key) mapped to city lat/lon.
Also fetches daily forecast high for comparison with market prices.

Usage:
    cd /Users/eric/polymarket-weather
    .venv/bin/python scripts/fetch_weather.py [--once]
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

REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH    = os.path.join(REPO_ROOT, "weather.db")

# City coordinates for open-meteo (matches WU settlement stations)
CITY_COORDS = {
    "Seoul":      (37.4602, 126.4407, "Asia/Seoul"),       # Incheon RKSI
    "Hong Kong":  (40.0799, 116.6031, "Asia/Shanghai"),    # ZBAA — Beijing coords (anomaly)
    "London":     (51.5053, 0.0553,   "Europe/London"),    # EGLC London City
    "Tokyo":      (35.5494, 139.7798, "Asia/Tokyo"),       # RJTT Haneda
    "NYC":        (40.7772, -73.8726, "America/New_York"), # KLGA LaGuardia
    "Paris":      (48.9694, 2.4414,   "Europe/Paris"),     # LFPB Le Bourget
    "Beijing":    (40.0799, 116.6031, "Asia/Shanghai"),    # ZBAA
    "Miami":      (25.7957, -80.2870, "America/New_York"), # KMIA
    "Singapore":  (1.3644,  103.9915, "Asia/Singapore"),   # WSSS Changi
    "Madrid":     (40.4719, -3.5626,  "Europe/Madrid"),    # LEMD Barajas
    "Moscow":     (60.3172, 24.9633,  "Europe/Helsinki"),  # EFHK — Helsinki (anomaly)
    "Munich":     (48.3538, 11.7861,  "Europe/Berlin"),    # EDDM
    "Amsterdam":  (52.3086, 4.7639,   "Europe/Amsterdam"), # EHAM Schiphol
    "Ankara":     (40.1281, 33.0011,  "Europe/Istanbul"),  # LTAC Esenboga
    "Wellington": (-41.3272, 174.8052,"Pacific/Auckland"), # NZWN
    "Shenzhen":   (22.6393, 113.8107, "Asia/Shanghai"),    # ZGSZ
    "Guangzhou":  (23.3924, 113.2988, "Asia/Shanghai"),    # ZGGG
}

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


async def fetch_city(client: httpx.AsyncClient, city: str) -> dict | None:
    if city not in CITY_COORDS:
        return None
    lat, lon, tz = CITY_COORDS[city]
    try:
        r = await client.get(OPEN_METEO, params={
            "latitude":             lat,
            "longitude":            lon,
            "timezone":             tz,
            "current":              "temperature_2m",
            "daily":                "temperature_2m_max,temperature_2m_min",
            "forecast_days":        2,
            "temperature_unit":     "celsius",
        }, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()

        current_temp = data.get("current", {}).get("temperature_2m")
        daily        = data.get("daily", {})
        daily_max    = (daily.get("temperature_2m_max") or [None])[0]
        daily_min    = (daily.get("temperature_2m_min") or [None])[0]
        tmrw_max     = (daily.get("temperature_2m_max") or [None, None])[1]

        return {
            "city":       city,
            "current_c":  current_temp,
            "today_high": daily_max,
            "today_low":  daily_min,
            "tmrw_high":  tmrw_max,
        }
    except Exception as e:
        log.warning("  %s: %s", city, e)
        return None


async def fetch_all(conn: sqlite3.Connection) -> int:
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    saved = 0

    async with httpx.AsyncClient() as client:
        tasks = [fetch_city(client, city) for city in CITY_COORDS]
        results = await asyncio.gather(*tasks)

    for obs in results:
        if not obs:
            continue
        city = obs["city"]

        # Log current observation
        conn.execute("""
            INSERT INTO wx_observations (station, city, ts_utc, temp_c, daily_high_c, source)
            VALUES (?, ?, ?, ?, ?, 'open-meteo')
        """, ("open-meteo", city, ts, obs["current_c"], obs["today_high"]))

        # Log/update today's forecast
        today = dt.date.today().isoformat()
        conn.execute("""
            INSERT INTO wx_forecasts (city, forecast_date, fetched_at_utc, high_c, low_c, source)
            VALUES (?,?,?,?,?,'open-meteo')
        """, (city, today, ts, obs["today_high"], obs["today_low"]))

        log.info("  %-12s  current=%.1f°C  today_high=%.1f°C  tmrw_high=%s°C",
                 city,
                 obs["current_c"] or 0,
                 obs["today_high"] or 0,
                 f"{obs['tmrw_high']:.1f}" if obs["tmrw_high"] else "n/a")
        saved += 1

    conn.commit()
    return saved


async def loop(interval_s: int = 900) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    log.info("Starting weather fetcher | interval=%ds (%.0f min)", interval_s, interval_s/60)
    while True:
        try:
            n = await fetch_all(conn)
            log.info("Fetched %d city readings", n)
        except Exception as e:
            log.exception("fetch error: %s", e)
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",     action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    args = parser.parse_args()

    if args.once:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        n = asyncio.run(fetch_all(conn))
        print(f"Fetched {n} city readings")
        conn.close()
    else:
        asyncio.run(loop(args.interval))
