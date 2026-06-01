"""
neg_risk_scanner.py
===================
Checks temperature markets for neg-risk inconsistencies:
  "above X°C" YES price ≈ sum of constituent "exact" YES prices above X

Also flags observation mismatches:
  if current observed temp > market's implied high, flag it.

Usage:
    cd /Users/eric/polymarket-weather
    .venv/bin/python scripts/neg_risk_scanner.py [--loop] [--interval 60]
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

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("wx_neg_risk")

REPO_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH        = os.path.join(REPO_ROOT, "weather.db")
GAP_THRESHOLD  = 0.015   # 1.5¢ — same as BTC scanner


def _load_markets(conn: sqlite3.Connection, settlement_date: str) -> list[dict]:
    rows = conn.execute("""
        SELECT
            condition_id, city, station, settlement_date,
            bucket_type, lower_temp, upper_temp, bucket_unit,
            yes_token_id, no_token_id, neg_risk_market_id, close_time_utc
        FROM weather_markets
        WHERE active=1
          AND settlement_date=?
          AND yes_token_id IS NOT NULL
          AND no_token_id IS NOT NULL
        ORDER BY city, lower_temp, upper_temp
    """, (settlement_date,)).fetchall()
    return [dict(r) for r in rows]


def _latest_obs(conn: sqlite3.Connection, city: str) -> float | None:
    row = conn.execute("""
        SELECT daily_high_c FROM wx_observations
        WHERE city=?
        ORDER BY fetched_utc DESC LIMIT 1
    """, (city,)).fetchone()
    return row[0] if row else None


def _attach_latest_prices(conn: sqlite3.Connection, markets: list[dict]) -> None:
    for market in markets:
        row = conn.execute("""
            SELECT yes_bid, yes_ask, yes_bid_size, yes_ask_size, ts_utc
            FROM ob_snapshots
            WHERE condition_id=?
            ORDER BY ts_utc DESC
            LIMIT 1
        """, (market["condition_id"],)).fetchone()
        if not row:
            market["yes_price"] = None
            market["best_size"] = None
            continue
        bid = row["yes_bid"]
        ask = row["yes_ask"]
        if bid is not None and ask is not None:
            market["yes_price"] = round((bid + ask) / 2, 4)
            market["best_size"] = min(
                row["yes_bid_size"] or 0,
                row["yes_ask_size"] or 0,
            )
        elif ask is not None:
            market["yes_price"] = ask
            market["best_size"] = row["yes_ask_size"]
        elif bid is not None:
            market["yes_price"] = bid
            market["best_size"] = row["yes_bid_size"]
        else:
            market["yes_price"] = None
            market["best_size"] = None
        market["price_ts_utc"] = row["ts_utc"]


def _unit_value_from_c(temp_c: float, unit: str) -> float:
    if unit == "F":
        return temp_c * 9 / 5 + 32
    return temp_c


def _parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _bucket_impossible_after_high(market: dict, high: float) -> bool:
    kind = market["bucket_type"]
    lower = market["lower_temp"]
    upper = market["upper_temp"]
    if kind in {"exact", "range", "below_eq"}:
        return upper is not None and high > upper
    return False


def _bucket_label(market: dict) -> str:
    unit = market["bucket_unit"]
    if market["bucket_type"] == "range":
        return f"{market['lower_temp']:.0f}-{market['upper_temp']:.0f}{unit}"
    if market["bucket_type"] == "above_eq":
        return f">={market['lower_temp']:.0f}{unit}"
    if market["bucket_type"] == "below_eq":
        return f"<={market['upper_temp']:.0f}{unit}"
    return f"{market['lower_temp']:.0f}{unit}"


def _check_neg_risk(markets: list[dict], conn: sqlite3.Connection) -> list[dict]:
    gaps = []
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    now = dt.datetime.now(dt.timezone.utc)

    cities = set(m["city"] for m in markets)
    for city in sorted(cities):
        city_mkts = [m for m in markets if m["city"] == city and m["yes_price"] is not None]

        above = {m["lower_temp"]: m for m in city_mkts if m["bucket_type"] == "above_eq"}
        finite_buckets = sorted(
            [m for m in city_mkts if m["bucket_type"] in {"exact", "range"}],
            key=lambda m: (m["lower_temp"] or -999, m["upper_temp"] or -999),
        )

        # Neg-risk check
        for strike, above_mkt in above.items():
            constituent_sum = sum(
                m["yes_price"] for m in finite_buckets
                if m["lower_temp"] is not None and m["lower_temp"] >= strike
            )
            if constituent_sum == 0:
                continue
            above_price = above_mkt["yes_price"]
            gap = abs(above_price - constituent_sum)
            direction = "above_cheap" if above_price < constituent_sum else "above_expensive"

            flag = "🚨 GAP" if gap > GAP_THRESHOLD else "   ok"
            log.info("%s  %-12s above %.0f°C  above=%.4f  sum=%.4f  gap=%+.4f",
                     flag, city, strike, above_price, constituent_sum,
                     gap if above_price > constituent_sum else -gap)

            if gap > GAP_THRESHOLD:
                entry = {"ts": ts, "city": city, "strike": strike,
                         "above_price": above_price, "bucket_sum": round(constituent_sum, 4),
                         "gap": round(gap, 4), "gap_direction": direction}
                gaps.append(entry)
                conn.execute("""
                    INSERT INTO alerts (ts_utc, city, alert_type, detail_json)
                    VALUES (?,?,?,?)
                """, (ts, city, "neg_risk_gap", json.dumps(entry)))

        # Observation mismatch check
        obs_temp = _latest_obs(conn, city)
        if obs_temp is not None:
            high = _unit_value_from_c(obs_temp, city_mkts[0]["bucket_unit"])
            for m in city_mkts:
                close = _parse_utc(m.get("close_time_utc"))
                if close and now >= close:
                    continue
                if m["yes_price"] is not None and m["yes_price"] > 0.05:
                    if _bucket_impossible_after_high(m, high):
                        log.info("OBS  %-12s  high=%.1f%s invalidates %-8s still YES=%.3f",
                                 city, high, m["bucket_unit"], _bucket_label(m), m["yes_price"])
                        conn.execute("""
                            INSERT INTO alerts (ts_utc, city, alert_type, detail_json)
                            VALUES (?,?,?,?)
                        """, (ts, city, "obs_mismatch", json.dumps({
                            "ts": ts, "city": city,
                            "obs_temp_c": obs_temp,
                            "obs_temp_bucket_unit": high,
                            "bucket": _bucket_label(m),
                            "yes_price": m["yes_price"]
                        })))

    conn.commit()
    return gaps


async def run_once(conn: sqlite3.Connection, settlement_date: str) -> list[dict]:
    markets = _load_markets(conn, settlement_date)
    if not markets:
        log.warning("No markets for %s — run discover_markets.py first", settlement_date)
        return []

    log.info("Scanning %d markets for %s", len(markets), settlement_date)
    _attach_latest_prices(conn, markets)

    priced = [m for m in markets if m.get("yes_price") is not None]
    log.info("Priced %d / %d", len(priced), len(markets))

    return _check_neg_risk(markets, conn)


async def loop(interval_s: int = 60) -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row
    today = dt.date.today().isoformat()
    log.info("Starting neg-risk scanner | date=%s | interval=%ds | threshold=%.3f",
             today, interval_s, GAP_THRESHOLD)
    while True:
        try:
            gaps = await run_once(conn, today)
            if gaps:
                log.info("⚡ %d gap(s) found", len(gaps))
        except Exception as e:
            log.exception("scan error: %s", e)
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",     action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--date",     default=dt.date.today().isoformat())
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row

    if args.loop:
        asyncio.run(loop(args.interval))
    else:
        gaps = asyncio.run(run_once(conn, args.date))
        print(f"\n{len(gaps)} gap(s) above threshold ({GAP_THRESHOLD})")
    conn.close()
