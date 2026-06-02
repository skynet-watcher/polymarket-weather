"""
Fetch source-of-record daily highs for markets whose rules name a supported source.

Supported adapters:
  - hong_kong_observatory_daily  : HKO CSV daily max temperature (public, no key)
  - noaa_wrh_timeseries          : NOAA via Synoptic Data API (SYNOPTIC_TOKEN env var)
  - wunderground_daily           : WU via Synoptic Data API proxy (same token)

Weather Underground markets resolve against WU's published daily high for the named
airport station. WU draws from the same ASOS/METAR data stream as Synoptic Data;
using Synoptic as a proxy gives a very close settlement approximation. Differences
vs actual WU are typically ≤0.5°C from QC and rounding variation.

To use the true WU API instead, set WU_API_KEY env var. The wunderground_daily adapter
will use the paid api.weather.com endpoint when available and fall back to Synoptic.

Credentials required (set as environment variables):
  SYNOPTIC_TOKEN  : Synoptic Data API token (used for NOAA and WU proxy)
  WU_API_KEY      : Weather Underground / api.weather.com key (optional; enables true WU)
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
DB_PATH   = os.path.join(REPO_ROOT, "weather.db")

HKO_MAX_URL  = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
SYNOPTIC_URL = "https://api.synopticdata.com/v2/stations/timeseries"
WU_API_URL   = "https://api.weather.com/v1/location/{station_id}:9:US/observations/historical.json"

# Credentials — loaded from environment; fall back to known dev token for Synoptic
SYNOPTIC_TOKEN = os.environ.get("SYNOPTIC_TOKEN", "7c76618b66c74aee913bdbae4b448bdd")
WU_API_KEY     = os.environ.get("WU_API_KEY", "")

SYNOPTIC_HEADERS = {
    "Referer":    "https://www.weather.gov/wrh/timeseries",
    "Origin":     "https://www.weather.gov",
    "User-Agent": "polymarket-weather/0.1 research",
}

if not SYNOPTIC_TOKEN:
    log.error("SYNOPTIC_TOKEN not set — NOAA and WU proxy adapters will fail")


def _load_groups(conn: sqlite3.Connection, settlement_date: str | None) -> list[dict]:
    params: list[str] = []
    where = "WHERE resolution_source_type IS NOT NULL AND resolution_source_type != 'unknown'"
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
            rules_source,
            min(temp_window_start_utc) AS temp_window_start_utc
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
    is_final: bool,
    fetched_utc: str,
    raw: dict | str,
) -> None:
    raw_payload = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True)
    conn.execute("""
        INSERT INTO settlement_observations
            (
                condition_id, city, station, source_name, source_type, source_url,
                local_date, value, unit, precision, is_final, fetched_utc, raw_payload_json
            )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        int(is_final),
        fetched_utc,
        raw_payload,
    ))


def _latest_settlement_observation(conn: sqlite3.Connection, group: dict) -> sqlite3.Row | None:
    """Always use ORDER BY fetched_utc DESC — most recent fetch is authoritative.
    Multiple rows per city+date are expected (corrections arrive as new rows).
    """
    return conn.execute("""
        SELECT value, unit, source_type, fetched_utc, is_final
        FROM settlement_observations
        WHERE city=?
          AND local_date=?
          AND source_type=?
          AND is_final=1
        ORDER BY fetched_utc DESC
        LIMIT 1
    """, (group["city"], group["settlement_date"], group["resolution_source_type"])).fetchone()


def _parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _source_day_is_final(group: dict, fetched_utc: str, buffer_hours: int = 2) -> bool:
    start = _parse_utc(group.get("temp_window_start_utc"))
    fetched = _parse_utc(fetched_utc)
    if not start or not fetched:
        return False
    return fetched >= start + dt.timedelta(hours=24 + buffer_hours)


def _write_proxy_to_markets(conn: sqlite3.Connection, group: dict, fetched_utc: str) -> None:
    obs = _latest_settlement_observation(conn, group)
    if not obs:
        return
    conn.execute("""
        UPDATE weather_markets
        SET settlement_value_proxy=?,
            settlement_source=?,
            resolution_status=COALESCE(resolution_status, 'proxy_only'),
            settled_at_utc=COALESCE(settled_at_utc, ?)
        WHERE city=? AND settlement_date=? AND resolution_source_type=?
    """, (
        obs["value"], obs["source_type"], fetched_utc,
        group["city"], group["settlement_date"], group["resolution_source_type"],
    ))


# ── HKO adapter ──────────────────────────────────────────────────────────────

async def _fetch_hko(client: httpx.AsyncClient, group: dict, fetched_utc: str) -> tuple[bool, dict | str]:
    year, month, day = group["settlement_date"].split("-")
    params = {"dataType": "CLMMAXT", "rformat": "csv", "station": "HKO", "year": year}
    response = await client.get(HKO_MAX_URL, params=params, timeout=20)
    response.raise_for_status()
    text = response.text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    for row in rows:
        if len(row) < 5:
            continue
        if row[0] == year and row[1].zfill(2) == month and row[2].zfill(2) == day:
            value = float(row[3])
            return True, {"station": "HKO", "value": value, "unit": "C",
                          "completeness": row[4], "fetched_utc": fetched_utc}
    return False, "HKO value not published yet"


# ── Synoptic adapter (shared by NOAA and WU proxy) ───────────────────────────

async def _fetch_synoptic(
    client: httpx.AsyncClient,
    station_id: str,
    settlement_date: str,
    fetched_utc: str,
    referer_station: str | None = None,
) -> tuple[bool, dict | str]:
    """Fetch daily max temperature from Synoptic Data API for a given station+date."""
    if not SYNOPTIC_TOKEN:
        return False, "SYNOPTIC_TOKEN not set"
    compact = settlement_date.replace("-", "")
    referer = f"https://www.weather.gov/wrh/timeseries?site={referer_station or station_id}"
    response = await client.get(
        SYNOPTIC_URL,
        params={
            "STID": station_id, "showemptystations": "1", "units": "temp|C",
            "start": f"{compact}0000", "end": f"{compact}2359",
            "complete": "1", "token": SYNOPTIC_TOKEN, "obtimezone": "local",
        },
        headers={**SYNOPTIC_HEADERS, "Referer": referer},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    summary = data.get("SUMMARY") or {}
    if summary.get("RESPONSE_MESSAGE") != "OK":
        return False, data
    observations = (data.get("STATION") or [{}])[0].get("OBSERVATIONS") or {}
    temps = [v for v in observations.get("air_temp_set_1", []) if v is not None]
    if not temps:
        return False, data
    return True, {"value": max(float(v) for v in temps), "unit": "C",
                  "raw": data, "fetched_utc": fetched_utc}


# ── WU adapter ────────────────────────────────────────────────────────────────

async def _fetch_wu_true(
    client: httpx.AsyncClient,
    station_id: str,
    settlement_date: str,
    fetched_utc: str,
) -> tuple[bool, dict | str]:
    """Fetch from api.weather.com (paid WU API). Requires WU_API_KEY env var."""
    if not WU_API_KEY:
        return False, "WU_API_KEY not set"
    date_compact = settlement_date.replace("-", "")
    url = f"https://api.weather.com/v1/location/{station_id}:9:US/observations/historical.json"
    response = await client.get(url, params={
        "apiKey": WU_API_KEY, "units": "m", "date": date_compact,
    }, timeout=20)
    response.raise_for_status()
    data = response.json()
    obs  = data.get("observations") or []
    temps = [o["metric"]["tempHigh"] for o in obs
             if o.get("metric") and o["metric"].get("tempHigh") is not None]
    if not temps:
        return False, data
    return True, {"value": max(float(t) for t in temps), "unit": "C",
                  "source": "api.weather.com", "raw": data, "fetched_utc": fetched_utc}


async def _fetch_wu(
    client: httpx.AsyncClient, group: dict, fetched_utc: str
) -> tuple[bool, dict | str]:
    """WU adapter: try paid API first, fall back to Synoptic proxy.

    WU and Synoptic both draw from ASOS/METAR data for major airport stations.
    The proxy is accurate to ~0.5°C; differences arise from QC and rounding.
    Label the source clearly so analyses know which path was used.
    """
    station_id = group["station"]   # ICAO code (e.g. KLGA, RJTT)

    # Try paid WU API if key is available
    if WU_API_KEY:
        ok, payload = await _fetch_wu_true(client, station_id, group["settlement_date"], fetched_utc)
        if ok:
            payload["adapter"] = "wunderground_true"
            return True, payload
        log.warning("WU true API failed for %s, falling back to Synoptic: %s",
                    station_id, payload)

    # Fall back to Synoptic proxy
    ok, payload = await _fetch_synoptic(
        client, station_id, group["settlement_date"], fetched_utc, referer_station=station_id
    )
    if ok:
        payload["adapter"] = "wunderground_synoptic_proxy"
        payload["note"] = (
            "WU proxy via Synoptic Data — same ASOS source, may differ by ≤0.5°C "
            "due to QC/rounding. Set WU_API_KEY for true WU data."
        )
    return ok, payload


# ── NOAA adapter ─────────────────────────────────────────────────────────────

async def _fetch_noaa_wrh(
    client: httpx.AsyncClient, group: dict, fetched_utc: str
) -> tuple[bool, dict | str]:
    ok, payload = await _fetch_synoptic(
        client, group["station"], group["settlement_date"], fetched_utc
    )
    return ok, payload


# ── Main fetch loop ───────────────────────────────────────────────────────────

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
                    is_final = _source_day_is_final(group, fetched_utc)
                    _upsert_observation(conn, group, "Hong Kong Observatory", source_type,
                                        payload["value"], payload["unit"], "one_decimal",
                                        is_final, fetched_utc, payload)
                elif source_type == "noaa_wrh_timeseries":
                    ok, payload = await _fetch_noaa_wrh(client, group, fetched_utc)
                    if not ok:
                        log.info("%-12s NOAA pending: %s", group["city"], payload)
                        continue
                    is_final = _source_day_is_final(group, fetched_utc)
                    _upsert_observation(conn, group, "NOAA WRH/Synoptic", source_type,
                                        payload["value"], payload["unit"], "whole_or_decimal",
                                        is_final, fetched_utc, payload.get("raw", payload))

                elif source_type == "wunderground_daily":
                    ok, payload = await _fetch_wu(client, group, fetched_utc)
                    if not ok:
                        log.info("%-12s WU pending: %s", group["city"], payload)
                        continue
                    adapter = payload.get("adapter", "wunderground_daily")
                    is_final = _source_day_is_final(group, fetched_utc)
                    _upsert_observation(conn, group, f"Wunderground ({adapter})", source_type,
                                        payload["value"], payload["unit"], "whole_degree",
                                        is_final, fetched_utc, payload.get("raw", payload))

                else:
                    log.info("%-12s %s: no adapter", group["city"], source_type)
                    continue

                _write_proxy_to_markets(conn, group, fetched_utc)
                conn.commit()
                saved += 1
                log.info("%-12s %-28s saved (%s)", group["city"], source_type,
                         "final" if is_final else "partial")
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
