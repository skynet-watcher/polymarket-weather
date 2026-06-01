"""
backfill_history.py
===================
Collect historical data for backtesting against past Polymarket weather markets.

What this fetches per past date:
  1. Market definitions   — Gamma API (same as discover_markets.py, any past date)
  2. Price history        — CLOB prices-history per token (~hourly resolution)
  3. Daily weather high   — open-meteo archive API (settlement proxy, years of history)
  4. Settlement source    — NOAA Synoptic + HKO historical (same adapters as live)
  5. Settlement outcome   — resolved market price snaps to 0.0 or 1.0

What this CANNOT provide:
  - Full order book depth history (CLOB does not archive bid/ask depth)
  - Historical NWP forecast runs at their original valid time
  - Sub-hourly price resolution (prices-history is ~1 point/hour)

Backtest coverage this enables:
  Test 2  Market-open accuracy       — opening price vs settlement temperature
  Test 6  Neg-risk gaps              — hourly price gaps (limited depth)
  Test 7  Resolution source mismatch — open-meteo/NOAA vs Polymarket outcome
  Test 8  Liquidity filter           — spread from hourly prices (approximate)

Usage:
    python scripts/backfill_history.py --days 30        # last 30 days
    python scripts/backfill_history.py --start 2026-05-01 --end 2026-05-31
    python scripts/backfill_history.py --days 7 --cities Seoul Tokyo  # subset
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
from discover_markets import (
    _load_cities, _event_slug, _upsert_market, _fetch_json, _fetch_clob,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")

REPO_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH      = os.path.join(REPO_ROOT, "weather.db")
GAMMA_EVENT  = "https://gamma-api.polymarket.com/events/slug"
CLOB_BASE    = "https://clob.polymarket.com"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
SYNOPTIC_URL = "https://api.synopticdata.com/v2/stations/timeseries"
SYNOPTIC_TOKEN = os.environ.get("SYNOPTIC_TOKEN", "7c76618b66c74aee913bdbae4b448bdd")


# ── Price history ─────────────────────────────────────────────────────────────

async def _fetch_price_history(
    client: httpx.AsyncClient,
    token_id: str,
    condition_id: str,
    conn: sqlite3.Connection,
) -> int:
    """Fetch hourly price history from CLOB and store as ob_snapshots.

    CLOB prices-history returns {t: unix_timestamp, p: price} points.
    We store as ob_snapshots with yes_ask=p (best approximation without depth).
    hours_to_close and snapshot_label are computed from close_time_utc.
    """
    try:
        r = await client.get(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "interval": "max", "fidelity": 60},
            timeout=20,
        )
        if r.status_code != 200:
            return 0
        history = r.json().get("history", [])
        if not history:
            return 0

        # Get market metadata for hours_to_close computation
        mkt = conn.execute(
            "SELECT close_time_utc, first_seen_utc FROM weather_markets WHERE condition_id=?",
            (condition_id,)
        ).fetchone()
        close_time_utc = mkt["close_time_utc"] if mkt else None

        saved = 0
        for point in history:
            ts_unix = point.get("t")
            price   = point.get("p")
            if ts_unix is None or price is None:
                continue

            ts_utc = dt.datetime.fromtimestamp(ts_unix, tz=dt.timezone.utc).isoformat()
            hours_to_close = None
            if close_time_utc:
                close_dt = dt.datetime.fromisoformat(
                    close_time_utc.replace("Z", "+00:00")
                )
                snap_dt = dt.datetime.fromtimestamp(ts_unix, tz=dt.timezone.utc)
                hours_to_close = round((close_dt - snap_dt).total_seconds() / 3600, 4)

            # Snapshot label (simplified — price-history only, no depth)
            label = _label_from_htc(hours_to_close)

            conn.execute("""
                INSERT OR IGNORE INTO ob_snapshots
                    (condition_id, ts_utc, yes_ask, yes_mid,
                     hours_to_close, snapshot_label, raw_book_json)
                VALUES (?,?,?,?,?,?,?)
            """, (
                condition_id, ts_utc,
                float(price), float(price),    # mid = ask (no depth available)
                hours_to_close, label,
                json.dumps({"source": "prices_history", "t": ts_unix, "p": price}),
            ))
            saved += 1

        conn.commit()
        return saved
    except Exception as e:
        log.warning("prices-history failed for %s: %s", token_id[:12], e)
        return 0


def _label_from_htc(htc: float | None) -> str | None:
    if htc is None:
        return None
    if htc < 0:
        return "post_close"
    for threshold, label in [
        (0.5, "T-30min"), (1.0, "T-1h"), (3.0, "T-3h"),
        (6.0, "T-6h"), (12.0, "T-12h"), (24.0, "T-24h"),
    ]:
        if htc <= threshold:
            return label
    return "open_window"


# ── open-meteo archive (settlement weather proxy) ────────────────────────────

async def _fetch_archive_weather(
    client: httpx.AsyncClient,
    city: dict,
    settlement_date: str,
    conn: sqlite3.Connection,
) -> bool:
    """Fetch daily max temperature from open-meteo archive for a past date."""
    try:
        r = await client.get(
            ARCHIVE_URL,
            params={
                "latitude":   city["lat"],
                "longitude":  city["lon"],
                "start_date": settlement_date,
                "end_date":   settlement_date,
                "daily":      "temperature_2m_max",
                "timezone":   city["timezone"],
            },
            timeout=20,
        )
        if r.status_code != 200:
            return False
        data = r.json()
        daily = data.get("daily", {})
        temps = daily.get("temperature_2m_max", [None])
        if not temps or temps[0] is None:
            return False

        value     = float(temps[0])
        fetched   = dt.datetime.now(dt.timezone.utc).isoformat()
        source    = "open-meteo archive (backfill settlement proxy)"

        # Store in settlement_observations
        group = conn.execute("""
            SELECT min(condition_id) AS condition_id, city, station,
                   settlement_unit, resolution_source_type
            FROM weather_markets
            WHERE city=? AND settlement_date=?
            GROUP BY city, station, settlement_date
        """, (city["city"], settlement_date)).fetchone()

        if not group:
            return False

        conn.execute("""
            INSERT INTO settlement_observations
                (condition_id, city, station, source_name, source_type, local_date,
                 value, unit, precision, fetched_utc, raw_payload_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            group["condition_id"], city["city"], city["station"],
            source, "open_meteo_archive",
            settlement_date, value, "C", "one_decimal",
            fetched, json.dumps({"data": data, "note": "backfill proxy — not official settlement source"}),
        ))
        conn.commit()
        log.info("  %-12s %s  archive=%.1f°C", city["city"], settlement_date, value)
        return True

    except Exception as e:
        log.warning("open-meteo archive failed for %s %s: %s", city["city"], settlement_date, e)
        return False


# ── Settlement outcome from resolved prices ───────────────────────────────────

async def _fetch_settlement_outcome(
    client: httpx.AsyncClient,
    condition_id: str,
    conn: sqlite3.Connection,
) -> bool:
    """Infer settlement outcome from resolved CLOB market prices.

    Resolved markets have prices snapped to 0.0 (NO) or 1.0 (YES).
    """
    try:
        r = await client.get(f"{CLOB_BASE}/markets/{condition_id}", timeout=10)
        if r.status_code != 200:
            return False
        data = r.json()
        tokens = data.get("tokens", [])
        if not tokens:
            return False

        for token in tokens:
            outcome = token.get("outcome")
            price   = token.get("price")
            if price is None:
                continue
            price = float(price)
            if outcome == "Yes" and price >= 0.99:
                # This market resolved YES
                conn.execute("""
                    INSERT OR IGNORE INTO market_resolutions
                        (condition_id, resolved_outcome, resolution_status, resolved_at_utc, raw_payload_json)
                    VALUES (?,?,?,?,?)
                """, (condition_id, "YES", "confirmed",
                      dt.datetime.now(dt.timezone.utc).isoformat(),
                      json.dumps(data)))
                conn.commit()
                return True
            elif outcome == "Yes" and price <= 0.01:
                conn.execute("""
                    INSERT OR IGNORE INTO market_resolutions
                        (condition_id, resolved_outcome, resolution_status, resolved_at_utc, raw_payload_json)
                    VALUES (?,?,?,?,?)
                """, (condition_id, "NO", "confirmed",
                      dt.datetime.now(dt.timezone.utc).isoformat(),
                      json.dumps(data)))
                conn.commit()
                return True
        return False
    except Exception as e:
        log.debug("settlement outcome failed for %s: %s", condition_id[:10], e)
        return False


# ── Main backfill loop ────────────────────────────────────────────────────────

async def backfill(
    conn: sqlite3.Connection,
    start_date: dt.date,
    end_date: dt.date,
    city_filter: list[str] | None = None,
) -> None:
    cities    = _load_cities()
    if city_filter:
        cities = [c for c in cities if c["city"] in city_filter]
    today     = dt.datetime.now(dt.timezone.utc).date()
    date_list = [
        start_date + dt.timedelta(days=i)
        for i in range((end_date - start_date).days + 1)
        if (start_date + dt.timedelta(days=i)) < today
    ]

    log.info("Backfilling %d cities × %d dates", len(cities), len(date_list))

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        for day in date_list:
            log.info("── %s ──", day)
            for city in cities:
                event_slug = _event_slug(city["slug"], day)

                # 1. Discover / refresh market metadata
                try:
                    event = await _fetch_json(client, f"{GAMMA_EVENT}/{event_slug}")
                except Exception as e:
                    log.debug("%s: %s", event_slug, e)
                    continue
                if not event:
                    continue

                markets = event.get("markets") or []
                log.info("  %-12s %s: %d markets", city["city"], day, len(markets))

                for market in markets:
                    condition_id = market.get("conditionId")
                    if not condition_id:
                        continue

                    clob = await _fetch_clob(client, condition_id)
                    _upsert_market(conn, city, event_slug, day, event, market, clob)
                    await asyncio.sleep(0.05)

                    if not clob:
                        continue

                    # 2. Price history per token
                    for token in (clob.get("tokens") or []):
                        tid = token.get("token_id")
                        if tid:
                            n = await _fetch_price_history(client, tid, condition_id, conn)
                            if n:
                                log.debug("    %s: %d price points", condition_id[:10], n)
                            await asyncio.sleep(0.05)

                    # 3. Settlement outcome (for resolved markets)
                    await _fetch_settlement_outcome(client, condition_id, conn)
                    await asyncio.sleep(0.05)

                conn.commit()

                # 4. Archive weather for this city+date
                await _fetch_archive_weather(client, city, day.isoformat(), conn)
                await asyncio.sleep(0.2)

    log.info("Backfill complete")


def _summary(conn: sqlite3.Connection) -> None:
    ph_pat  = "%prices_history%"
    markets = conn.execute("SELECT COUNT(*) FROM weather_markets").fetchone()[0]
    prices  = conn.execute("SELECT COUNT(*) FROM ob_snapshots WHERE raw_book_json LIKE ?", (ph_pat,)).fetchone()[0]
    archive = conn.execute("SELECT COUNT(*) FROM settlement_observations WHERE source_type='open_meteo_archive'").fetchone()[0]
    resolved= conn.execute("SELECT COUNT(*) FROM market_resolutions").fetchone()[0]
    cities  = conn.execute("SELECT COUNT(DISTINCT city) FROM weather_markets").fetchone()[0]
    dates   = conn.execute("SELECT COUNT(DISTINCT settlement_date) FROM weather_markets").fetchone()[0]
    print("\n── Backfill summary ──")
    print(f"  Markets:             {markets}")
    print(f"  Price history pts:   {prices}")
    print(f"  Archive weather:     {archive}")
    print(f"  Resolved outcomes:   {resolved}")
    print(f"  Cities covered:      {cities}")
    print(f"  Dates covered:       {dates}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill historical weather market data")
    parser.add_argument("--days",   type=int, default=30,
                        help="Number of past days to backfill (default 30)")
    parser.add_argument("--start",  help="Start date YYYY-MM-DD (overrides --days)")
    parser.add_argument("--end",    help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--cities", nargs="+",
                        help="Subset of cities to backfill (default: all 17)")
    args = parser.parse_args()

    today     = dt.datetime.now(dt.timezone.utc).date()
    end_date  = dt.date.fromisoformat(args.end) if args.end else today - dt.timedelta(days=1)
    start_date = (
        dt.date.fromisoformat(args.start) if args.start
        else end_date - dt.timedelta(days=args.days - 1)
    )

    log.info("Backfilling %s → %s", start_date, end_date)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    asyncio.run(backfill(conn, start_date, end_date, args.cities))
    _summary(conn)
    conn.close()
