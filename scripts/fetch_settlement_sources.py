"""
Fetch source-of-record daily highs for markets whose rules name a supported source.

Supported now:
- Hong Kong Observatory daily maximum temperature CSV
- NOAA WRH/Synoptic timeseries for NOAA-named airport stations

Weather Underground markets are intentionally left pending until a reliable WU adapter
is added. METAR remains the fast proxy, not the settlement source.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import os
import sqlite3
import sys

import httpx

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("settlement_sources")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "weather.db")

HKO_MAX_URL = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
SYNOPTIC_URL = "https://api.synopticdata.com/v2/stations/timeseries"
SYNOPTIC_TOKEN = "7c76618b66c74aee913bdbae4b448bdd"
SYNOPTIC_HEADERS = {
    "Referer": "https://www.weather.gov/wrh/timeseries",
    "Origin": "https://www.weather.gov",
    "User-Agent": "polymarket-weather/0.1 research",
}


def _load_groups(conn: sqlite3.Connection, settlement_date: str | None) -> list[dict]:
    params: list[str] = []
    where = "WHERE resolution_source_type IS NOT NULL"
    if settlement_date:
        where += " AND settlement_date=?"
        params.append(settlement_date)
    rows = conn.execute(f"""
        SELECT
            min(condition_id) AS condition_id,
            city,
            station,
            settlement_date,
            settlement_unit,
            resolution_source_type,
            resolution_source_url,
            rules_source
        FROM weather_markets
        {where}
        GROUP BY city, station, settlement_date, settlement_unit,
                 resolution_source_type, resolution_source_url, rules_source
        ORDER BY settlement_date, city
    """, params).fetchall()
    return [dict(row) for row in rows]


def _upsert_observation(
    conn: sqlite3.Connection,
    group: dict,
    source_name: str,
    source_type: str,
    value: float,
    unit: str,
    precision: str,
    fetched_utc: str,
    raw: dict | str,
) -> None:
    raw_payload = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True)
    conn.execute("""
        INSERT INTO settlement_observations
            (
                condition_id, city, station, source_name, source_type, source_url,
                local_date, value, unit, precision, fetched_utc, raw_payload_json
            )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        group["condition_id"],
        group["city"],
        group["station"],
        source_name,
        source_type,
        group.get("resolution_source_url"),
        group["settlement_date"],
        value,
        unit,
        precision,
        fetched_utc,
        raw_payload,
    ))


def _latest_settlement_observation(conn: sqlite3.Connection, group: dict) -> sqlite3.Row | None:
    return conn.execute("""
        SELECT value, unit, source_type, fetched_utc
        FROM settlement_observations
        WHERE city=?
          AND local_date=?
          AND source_type=?
        ORDER BY fetched_utc DESC
        LIMIT 1
    """, (
        group["city"],
        group["settlement_date"],
        group["resolution_source_type"],
    )).fetchone()


def _write_proxy_to_markets(conn: sqlite3.Connection, group: dict, fetched_utc: str) -> None:
    obs = _latest_settlement_observation(conn, group)
    if not obs:
        return
    conn.execute("""
        UPDATE weather_markets
        SET settlement_value_proxy=?,
            settlement_source=?,
            resolution_status='proxy_only',
            settled_at_utc=?
        WHERE city=?
          AND settlement_date=?
          AND resolution_source_type=?
    """, (
        obs["value"],
        obs["source_type"],
        fetched_utc,
        group["city"],
        group["settlement_date"],
        group["resolution_source_type"],
    ))


async def _fetch_hko(client: httpx.AsyncClient, group: dict, fetched_utc: str) -> tuple[bool, str]:
    year, month, day = group["settlement_date"].split("-")
    station = "HKO"
    params = {
        "dataType": "CLMMAXT",
        "rformat": "csv",
        "station": station,
        "year": year,
    }
    response = await client.get(HKO_MAX_URL, params=params, timeout=20)
    response.raise_for_status()
    text = response.text.lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(text)))
    for row in rows:
        if len(row) < 5:
            continue
        if row[0] == year and row[1].zfill(2) == month and row[2].zfill(2) == day:
            value = float(row[3])
            completeness = row[4]
            return True, json.dumps({
                "station": station,
                "value": value,
                "unit": "C",
                "completeness": completeness,
                "rows": rows[:3] + [row],
                "fetched_utc": fetched_utc,
            }, ensure_ascii=False)
    return False, "HKO value not published yet"


async def _fetch_noaa_wrh(client: httpx.AsyncClient, group: dict, fetched_utc: str) -> tuple[bool, dict | str]:
    compact_date = group["settlement_date"].replace("-", "")
    response = await client.get(
        SYNOPTIC_URL,
        params={
            "STID": group["station"],
            "showemptystations": "1",
            "units": "temp|C",
            "start": f"{compact_date}0000",
            "end": f"{compact_date}2359",
            "complete": "1",
            "token": SYNOPTIC_TOKEN,
            "obtimezone": "local",
        },
        headers={**SYNOPTIC_HEADERS, "Referer": f"https://www.weather.gov/wrh/timeseries?site={group['station']}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    summary = data.get("SUMMARY") or {}
    if summary.get("RESPONSE_MESSAGE") != "OK":
        return False, data
    observations = (data.get("STATION") or [{}])[0].get("OBSERVATIONS") or {}
    temps = [value for value in observations.get("air_temp_set_1", []) if value is not None]
    if not temps:
        return False, data
    return True, {
        "value": max(float(value) for value in temps),
        "unit": "C",
        "raw": data,
        "fetched_utc": fetched_utc,
    }


async def fetch_once(conn: sqlite3.Connection, settlement_date: str | None = None) -> int:
    groups = _load_groups(conn, settlement_date)
    if not groups:
        log.warning("No markets found for settlement source fetch")
        return 0

    fetched_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    saved = 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for group in groups:
            source_type = group["resolution_source_type"]
            try:
                if source_type == "hong_kong_observatory_daily":
                    ok, payload = await _fetch_hko(client, group, fetched_utc)
                    if not ok:
                        log.info("%-12s HKO pending: %s", group["city"], payload)
                        continue
                    parsed = json.loads(payload)
                    _upsert_observation(conn, group, "Hong Kong Observatory", source_type,
                                        parsed["value"], parsed["unit"], "one_decimal",
                                        fetched_utc, payload)
                elif source_type == "noaa_wrh_timeseries":
                    ok, payload = await _fetch_noaa_wrh(client, group, fetched_utc)
                    if not ok:
                        log.info("%-12s NOAA pending: %s", group["city"], payload)
                        continue
                    _upsert_observation(conn, group, "NOAA WRH/Synoptic", source_type,
                                        payload["value"], payload["unit"], "whole_or_decimal",
                                        fetched_utc, payload["raw"])
                else:
                    log.info("%-12s %s pending: adapter not implemented", group["city"], source_type)
                    continue
                _write_proxy_to_markets(conn, group, fetched_utc)
                conn.commit()
                saved += 1
                log.info("%-12s %-28s saved", group["city"], source_type)
            except Exception as exc:
                log.warning("%-12s %-28s failed: %s", group["city"], source_type, exc)
    return saved


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Settlement local date, YYYY-MM-DD")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row
    count = asyncio.run(fetch_once(conn, args.date))
    print(f"Saved {count} settlement-source observation(s)")
    conn.close()
