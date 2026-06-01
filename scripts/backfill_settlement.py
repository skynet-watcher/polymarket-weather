"""
backfill_settlement.py
======================
Collect authoritative historical settlement data for past markets.

PURPOSE: This script fetches the GROUND TRUTH for backtesting:
  1. Settlement temperature — from IEM ASOS (same data as WU for airports)
                              and open-meteo archive as universal fallback
  2. Settlement outcome    — which bucket resolved YES, from CLOB resolved prices
  3. Settlement temperature inferred from resolved bucket (for exact buckets)

IMPORTANT — why this is separate from backfill_history.py:
  backfill_history.py collects market structure and price history.
  This script focuses specifically on settlement correctness, which is
  the ground truth that all backtest signal analysis depends on.
  Getting settlement wrong invalidates every test that measures accuracy.

LIMITATION on historical forecast signals:
  True NWP forecast data at specific lead times (T-48h, T-24h, etc.) is NOT
  available from any free API for dates more than ~2 weeks ago. The open-meteo
  historical forecast API returns analysis values, not advance predictions.
  Forecast signal backtesting requires live forward collection (see fetch_weather.py).
  The system has been collecting forward data since launch.

Settlement source priority per market:
  1. Resolved CLOB outcome (bucket that snapped to 1.0) — most reliable
  2. IEM ASOS daily max     (US + some international airports, free, same as WU)
  3. HKO historical CSV     (Hong Kong only, monthly granularity)
  4. open-meteo archive     (universal proxy, good to ±0.5°C for airports)

Usage:
    python scripts/backfill_settlement.py --days 30
    python scripts/backfill_settlement.py --start 2026-05-01 --end 2026-05-31
    python scripts/backfill_settlement.py --days 7 --verify   # cross-check sources
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import io
import json
import logging
import os
import sqlite3
import sys
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("backfill_settlement")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH   = os.path.join(REPO_ROOT, "weather.db")

IEM_URL      = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
HKO_URL      = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
CLOB_BASE    = "https://clob.polymarket.com"

# IEM station codes — US stations use 3-letter FAA, international use ICAO
# IEM only has full coverage for US and a subset of international
IEM_STATIONS = {
    "RKSI": "RKSI",   # Seoul Incheon ✅
    "RJTT": "RJTT",   # Tokyo Haneda ✅
    "ZBAA": "ZBAA",   # Beijing ✅
    "ZGSZ": "ZGSZ",   # Shenzhen ✅
    "ZGGG": "ZGGG",   # Guangzhou ✅
    "WSSS": "WSSS",   # Singapore ✅
    "KLGA": "LGA",    # NYC LaGuardia (US: drop K)
    "KMIA": "MIA",    # Miami (US: drop K)
    # European and other international stations not reliably in IEM
}


# ── Resolved outcome from CLOB ────────────────────────────────────────────────

async def _fetch_resolved_outcome(
    client: httpx.AsyncClient,
    condition_id: str,
) -> tuple[str | None, float | None]:
    """Return (resolved_outcome, resolved_price) for a past market.

    Resolved markets: YES token price ≈ 1.0, NO token price ≈ 0.0.
    Returns ('YES'|'NO', price) or (None, None) if unresolved/unavailable.
    """
    try:
        r = await client.get(f"{CLOB_BASE}/markets/{condition_id}", timeout=10)
        if r.status_code != 200:
            return None, None
        data  = r.json()
        tokens = data.get("tokens") or []
        for token in tokens:
            if token.get("outcome") == "Yes":
                price = float(token.get("price", 0))
                if price >= 0.99:
                    return "YES", price
                elif price <= 0.01:
                    return "NO", price
        return None, None   # unresolved
    except Exception as e:
        log.debug("CLOB outcome %s: %s", condition_id[:10], e)
        return None, None


def _infer_settlement_temp_from_outcome(
    conn: sqlite3.Connection,
    city: str,
    settlement_date: str,
) -> float | None:
    """Infer settlement temperature from which bucket resolved YES.

    For exact Celsius buckets, the settlement temp equals the bucket value.
    For ranges, it falls somewhere within [lower, upper].
    Returns None if outcome not in DB or bucket is ambiguous.
    """
    yes_row = conn.execute("""
        SELECT bucket_type, lower_temp, upper_temp, bucket_unit
        FROM weather_markets wm
        JOIN market_resolutions mr USING (condition_id)
        WHERE wm.city=? AND wm.settlement_date=?
          AND mr.resolved_outcome='YES'
          AND wm.bucket_type='exact'
        LIMIT 1
    """, (city, settlement_date)).fetchone()

    if yes_row and yes_row["bucket_unit"] == "C":
        return float(yes_row["lower_temp"])
    return None


# ── IEM ASOS historical ───────────────────────────────────────────────────────

async def _fetch_iem_daily_max(
    client: httpx.AsyncClient,
    station_icao: str,
    settlement_date: str,
    station_tz: str,
) -> float | None:
    """Fetch daily max temperature from IEM ASOS.

    IEM provides the same ASOS/METAR data stream as WU for airport stations.
    Returns °C daily max over the local calendar day, or None if unavailable.
    """
    iem_id = IEM_STATIONS.get(station_icao)
    if not iem_id:
        return None

    y, m, d = settlement_date.split("-")
    try:
        r = await client.get(IEM_URL, params={
            "station": iem_id, "data": "tmpc",
            "year1": y, "month1": m, "day1": d,
            "year2": y, "month2": m, "day2": d,
            "tz": "UTC", "format": "onlycomma",
            "latlon": "no", "missing": "null",
        }, timeout=20)
        if r.status_code != 200:
            return None

        # Filter observations to local calendar day
        tz   = ZoneInfo(station_tz)
        date = dt.date.fromisoformat(settlement_date)
        day_start = dt.datetime(date.year, date.month, date.day, tzinfo=tz).astimezone(dt.timezone.utc)
        day_end   = day_start + dt.timedelta(days=1)

        temps = []
        for line in r.text.strip().splitlines():
            if line.startswith("station"):
                continue
            parts = line.split(",")
            if len(parts) < 4 or parts[3] in ("null", "M", ""):
                continue
            try:
                ts = dt.datetime.strptime(
                    parts[1], "%Y-%m-%d %H:%M"
                ).replace(tzinfo=dt.timezone.utc)
                if day_start <= ts < day_end:
                    temps.append(float(parts[3]))
            except Exception:
                continue

        return max(temps) if temps else None

    except Exception as e:
        log.debug("IEM %s %s: %s", station_icao, settlement_date, e)
        return None


# ── open-meteo archive ────────────────────────────────────────────────────────

async def _fetch_archive_max(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    settlement_date: str,
    timezone: str,
) -> float | None:
    """Fetch daily max from open-meteo archive (universal fallback)."""
    try:
        r = await client.get(ARCHIVE_URL, params={
            "latitude": lat, "longitude": lon,
            "start_date": settlement_date, "end_date": settlement_date,
            "daily": "temperature_2m_max", "timezone": timezone,
        }, timeout=20)
        if r.status_code != 200:
            return None
        temps = r.json().get("daily", {}).get("temperature_2m_max", [None])
        return float(temps[0]) if temps and temps[0] is not None else None
    except Exception as e:
        log.debug("open-meteo archive %s: %s", settlement_date, e)
        return None


# ── HKO historical ────────────────────────────────────────────────────────────

async def _fetch_hko_historical(
    client: httpx.AsyncClient,
    settlement_date: str,
) -> float | None:
    """Fetch HKO daily max temperature for a past date."""
    y, m, d = settlement_date.split("-")
    try:
        r = await client.get(HKO_URL, params={
            "dataType": "CLMMAXT", "rformat": "csv", "station": "HKO", "year": y,
        }, timeout=20)
        if r.status_code != 200:
            return None
        rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
        for row in rows:
            if (len(row) >= 4 and row[0] == y
                    and row[1].zfill(2) == m and row[2].zfill(2) == d):
                return float(row[3])
        return None
    except Exception as e:
        log.debug("HKO %s: %s", settlement_date, e)
        return None


# ── Store settlement observation ──────────────────────────────────────────────

def _store_settlement(
    conn: sqlite3.Connection,
    condition_id: str,
    city: str,
    station: str,
    settlement_date: str,
    resolution_source_type: str,
    value: float,
    unit: str,
    source_name: str,
    fetched_utc: str,
    raw: dict,
) -> None:
    conn.execute("""
        INSERT INTO settlement_observations
            (condition_id, city, station, source_name, source_type,
             local_date, value, unit, precision, fetched_utc, raw_payload_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        condition_id, city, station, source_name, resolution_source_type,
        settlement_date, value, unit, "one_decimal_or_whole",
        fetched_utc, json.dumps(raw),
    ))


# ── Main ──────────────────────────────────────────────────────────────────────

async def backfill_settlement(
    conn: sqlite3.Connection,
    start_date: dt.date,
    end_date: dt.date,
    verify: bool = False,
) -> None:
    from discover_markets import _load_cities
    cities = {c["city"]: c for c in _load_cities()}

    # Get all past city+dates in the DB for the date range
    rows = conn.execute("""
        SELECT DISTINCT city, station, settlement_date, resolution_source_type,
               min(condition_id) AS condition_id
        FROM weather_markets
        WHERE settlement_date >= ? AND settlement_date <= ?
          AND bucket_type != 'unknown'
        GROUP BY city, station, settlement_date, resolution_source_type
        ORDER BY settlement_date, city
    """, (start_date.isoformat(), end_date.isoformat())).fetchall()

    log.info("Processing %d city-date groups", len(rows))
    fetched_utc = dt.datetime.now(dt.timezone.utc).isoformat()

    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for row in rows:
            city         = row["city"]
            station      = row["station"]
            sdate        = row["settlement_date"]
            cid          = row["condition_id"]
            src_type     = row["resolution_source_type"] or "wunderground_daily"
            city_meta    = cities.get(city, {})

            log.info("%-12s %s (%s)", city, sdate, src_type)

            # 1. Get resolved outcome from CLOB — most reliable ground truth
            #    Fetch all bucket outcomes for this city+date
            market_rows = conn.execute("""
                SELECT condition_id, bucket_type, lower_temp, upper_temp, bucket_unit,
                       yes_token_id, no_token_id
                FROM weather_markets
                WHERE city=? AND settlement_date=?
                  AND yes_token_id IS NOT NULL
                ORDER BY lower_temp
            """, (city, sdate)).fetchall()

            resolved_count = 0
            inferred_temp  = None
            for mkt in market_rows:
                outcome, price = await _fetch_resolved_outcome(client, mkt["condition_id"])
                if outcome:
                    conn.execute("""
                        INSERT OR IGNORE INTO market_resolutions
                            (condition_id, resolved_outcome, resolution_status,
                             resolved_at_utc, raw_payload_json)
                        VALUES (?,?,?,?,?)
                    """, (mkt["condition_id"], outcome, "confirmed",
                          fetched_utc, json.dumps({"price": price})))
                    resolved_count += 1
                    # Infer settlement temp from exact Celsius bucket
                    if (outcome == "YES" and mkt["bucket_type"] == "exact"
                            and mkt["bucket_unit"] == "C"):
                        inferred_temp = float(mkt["lower_temp"])
                await asyncio.sleep(0.03)

            if resolved_count:
                log.info("  → %d outcomes resolved; inferred_temp=%s°C",
                         resolved_count, inferred_temp)
            conn.commit()

            # 2. Settlement temperature — try sources in priority order
            settlement_c  = None
            source_used   = None

            # 2a. IEM ASOS (same data as WU for supported airports)
            tz = city_meta.get("timezone", "UTC")
            iem_temp = await _fetch_iem_daily_max(client, station, sdate, tz)
            if iem_temp is not None:
                settlement_c = iem_temp
                source_used  = "IEM ASOS (WU-equivalent)"
                log.info("  IEM:  %.1f°C", iem_temp)

            # 2b. HKO for Hong Kong
            if city == "Hong Kong" and settlement_c is None:
                hko_temp = await _fetch_hko_historical(client, sdate)
                if hko_temp is not None:
                    settlement_c = hko_temp
                    source_used  = "HKO historical"
                    log.info("  HKO:  %.1f°C", hko_temp)

            # 2c. open-meteo archive as universal fallback
            if settlement_c is None:
                lat = city_meta.get("lat")
                lon = city_meta.get("lon")
                if lat and lon:
                    arch_temp = await _fetch_archive_max(client, lat, lon, sdate, tz)
                    if arch_temp is not None:
                        settlement_c = arch_temp
                        source_used  = "open-meteo archive (proxy)"
                        log.info("  Archive: %.1f°C", arch_temp)

            # Store settlement temperature
            if settlement_c is not None:
                _store_settlement(
                    conn, cid, city, station, sdate, src_type,
                    settlement_c, "C", source_used, fetched_utc,
                    {"source": source_used, "value": settlement_c,
                     "inferred_exact_temp": inferred_temp},
                )
                conn.commit()

            # 2d. Cross-validation in --verify mode
            if verify and settlement_c is not None and inferred_temp is not None:
                diff = abs(settlement_c - inferred_temp)
                flag = "✅" if diff <= 1.0 else "⚠️ "
                log.info("  %s Settlement proxy vs resolved bucket: %.1f°C vs %.0f°C (diff=%.1f)",
                         flag, settlement_c, inferred_temp, diff)

            await asyncio.sleep(0.2)

    # Final summary
    resolved  = conn.execute("SELECT COUNT(*) FROM market_resolutions").fetchone()[0]
    obs       = conn.execute("SELECT COUNT(*) FROM settlement_observations").fetchone()[0]
    iem_obs   = conn.execute(
        "SELECT COUNT(*) FROM settlement_observations WHERE source_name LIKE '%IEM%'"
    ).fetchone()[0]
    arch_obs  = conn.execute(
        "SELECT COUNT(*) FROM settlement_observations WHERE source_name LIKE '%archive%'"
    ).fetchone()[0]

    print(f"\n── Settlement backfill summary ──")
    print(f"  Resolved outcomes written:  {resolved}")
    print(f"  Settlement temps total:     {obs}")
    print(f"    IEM ASOS (WU-equivalent): {iem_obs}")
    print(f"    open-meteo archive proxy: {arch_obs}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill authoritative settlement data for past markets"
    )
    parser.add_argument("--days",   type=int, default=30,
                        help="Past days to process (default 30)")
    parser.add_argument("--start",  help="Start date YYYY-MM-DD")
    parser.add_argument("--end",    help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--verify", action="store_true",
                        help="Cross-check settlement proxy vs resolved bucket outcome")
    args = parser.parse_args()

    today      = dt.datetime.now(dt.timezone.utc).date()
    end_date   = dt.date.fromisoformat(args.end)   if args.end   else today - dt.timedelta(days=1)
    start_date = dt.date.fromisoformat(args.start) if args.start else end_date - dt.timedelta(days=args.days - 1)

    log.info("Settlement backfill %s → %s", start_date, end_date)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    asyncio.run(backfill_settlement(conn, start_date, end_date, args.verify))
    conn.close()
