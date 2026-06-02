"""
backfill_history.py
===================
Collect historical data for backtesting against past Polymarket weather markets.

What this fetches per past date:
  1. Market definitions   — Gamma API (same as discover_markets.py, any past date)
  2. Price history        — CLOB prices-history per token (~hourly resolution),
                            stored in market_price_history (not ob_snapshots)
  3. Settlement outcome   — resolved market price snaps to 0.0 or 1.0
  4. Archive weather      — open-meteo archive daily max (universal settlement proxy)

For authoritative settlement temperatures, run backfill_settlement.py AFTER this
script — it fetches IEM ASOS (WU-equivalent), HKO historical, and open-meteo archive
with proper source labelling and cross-validation.

Historical NWP forecast signals:
  Do not use the open-meteo archive API for forecast backtests; it returns analysis
  values, not advance predictions. Use scripts/backfill_model_forecasts.py instead.
  It pulls Open-Meteo Previous Runs and stores fixed-lead model forecasts in
  model_forecasts. True market-open tests still require market_start_utc/first_seen_utc
  and live order books.

What this backfill CAN support for backtesting:
  Test 6  Neg-risk gaps              — hourly price history shows past gap windows
  Test 7  Resolution source mismatch — archive proxy vs resolved Polymarket outcome
  Test 8  Liquidity filter           — NOT supported by price-history backfill; requires
                                       live order book snapshots with bid/ask/size
  Test 2  Market-open accuracy       — requires scripts/backfill_model_forecasts.py
                                       for historical forecasts, plus live/book data
                                       for executable open prices.

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
    outcome: str | None = None,
) -> int:
    """Fetch hourly price history from CLOB and store as market_price_history.

    CLOB prices-history returns {t: unix_timestamp, p: price} points.
    It is not an order book: it has no bid, ask, spread, size, or depth.
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

        saved = 0
        for point in history:
            ts_unix = point.get("t")
            price   = point.get("p")
            if ts_unix is None or price is None:
                continue

            ts_utc = dt.datetime.fromtimestamp(ts_unix, tz=dt.timezone.utc).isoformat()
            conn.execute("""
                INSERT OR IGNORE INTO market_price_history
                    (token_id, condition_id, outcome, ts_utc, price,
                     fidelity_minutes, source, raw_payload_json)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                token_id, condition_id, outcome, ts_utc, float(price),
                60, "clob_prices_history",
                json.dumps({"t": ts_unix, "p": price, "token_id": token_id, "outcome": outcome}),
            ))
            saved += 1

        conn.commit()
        return saved
    except Exception as e:
        log.warning("prices-history failed for %s: %s", token_id[:12], e)
        return 0


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
    clob_data: dict | None = None,
) -> bool:
    """Infer settlement outcome from resolved CLOB market prices.

    Resolved markets have prices snapped to 0.0 (NO) or 1.0 (YES).
    """
    try:
        if clob_data is None:
            r = await client.get(f"{CLOB_BASE}/markets/{condition_id}", timeout=10)
            if r.status_code != 200:
                return False
            data = r.json()
        else:
            data = clob_data
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


def _infer_gamma_settlement_outcome(
    market: dict,
    condition_id: str,
    conn: sqlite3.Connection,
) -> bool:
    """Infer settlement from Gamma outcomePrices when CLOB detail is skipped."""
    try:
        raw_outcomes = market.get("outcomes")
        raw_prices = market.get("outcomePrices")
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        if not outcomes or not prices:
            return False

        pairs = dict(zip(outcomes, prices))
        yes_price = pairs.get("Yes")
        if yes_price is None:
            return False

        yes_price = float(yes_price)
        if yes_price >= 0.99:
            outcome = "YES"
        elif yes_price <= 0.01:
            outcome = "NO"
        else:
            return False

        conn.execute("""
            INSERT OR IGNORE INTO market_resolutions
                (condition_id, resolved_outcome, resolution_status, resolved_at_utc, raw_payload_json)
            VALUES (?,?,?,?,?)
        """, (
            condition_id, outcome, "confirmed",
            dt.datetime.now(dt.timezone.utc).isoformat(),
            json.dumps({"gamma": market, "settlement_source": "gamma_outcomePrices"}),
        ))
        return True
    except Exception as e:
        log.debug("gamma settlement outcome failed for %s: %s", condition_id[:10], e)
        return False


# ── Main backfill loop ────────────────────────────────────────────────────────

async def backfill(
    conn: sqlite3.Connection,
    start_date: dt.date,
    end_date: dt.date,
    city_filter: list[str] | None = None,
    fetch_price_history: bool = True,
    fetch_clob_markets: bool = True,
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

                    clob = await _fetch_clob(client, condition_id) if fetch_clob_markets else None
                    _upsert_market(conn, city, event_slug, day, event, market, clob, "backfill")
                    await asyncio.sleep(0.05)

                    if not clob:
                        _infer_gamma_settlement_outcome(market, condition_id, conn)
                        continue

                    # 2. Price history per token. Optional because this is slow and
                    # not needed for forecast-vs-resolution research.
                    if fetch_price_history:
                        for token in (clob.get("tokens") or []):
                            tid = token.get("token_id")
                            if tid:
                                n = await _fetch_price_history(
                                    client, tid, condition_id, conn, token.get("outcome")
                                )
                                if n:
                                    log.debug("    %s: %d price points", condition_id[:10], n)
                                await asyncio.sleep(0.05)

                    # 3. Settlement outcome (for resolved markets)
                    await _fetch_settlement_outcome(client, condition_id, conn, clob)
                    await asyncio.sleep(0.05)

                conn.commit()

                # 4. Archive weather for this city+date
                await _fetch_archive_weather(client, city, day.isoformat(), conn)
                await asyncio.sleep(0.2)

    log.info("Backfill complete")


def _summary(conn: sqlite3.Connection) -> None:
    markets = conn.execute("SELECT COUNT(*) FROM weather_markets").fetchone()[0]
    prices  = conn.execute("SELECT COUNT(*) FROM market_price_history").fetchone()[0]
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
    parser.add_argument("--skip-price-history", action="store_true",
                        help="Skip slow CLOB prices-history; still fetch markets, outcomes, and archive weather")
    parser.add_argument("--skip-clob-markets", action="store_true",
                        help="Skip per-condition CLOB market fetches; infer resolved outcomes from Gamma outcomePrices")
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

    asyncio.run(backfill(
        conn,
        start_date,
        end_date,
        args.cities,
        not args.skip_price_history,
        not args.skip_clob_markets,
    ))
    _summary(conn)
    conn.close()
