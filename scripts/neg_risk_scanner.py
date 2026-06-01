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

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("wx_neg_risk")

REPO_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH        = os.path.join(REPO_ROOT, "weather.db")
GAP_THRESHOLD  = 0.015   # 1.5¢ — same as BTC scanner
CLOB_BASE      = "https://clob.polymarket.com"


def _load_markets(conn: sqlite3.Connection, settlement_date: str) -> list[dict]:
    rows = conn.execute("""
        SELECT condition_id, city, station, settlement_date, bucket_type, temp_c, yes_token_id
        FROM weather_markets
        WHERE active=1 AND settlement_date=? AND yes_token_id IS NOT NULL
        ORDER BY city, temp_c
    """, (settlement_date,)).fetchall()
    return [dict(r) for r in rows]


def _latest_obs(conn: sqlite3.Connection, city: str) -> float | None:
    row = conn.execute("""
        SELECT temp_c FROM wx_observations
        WHERE city=?
        ORDER BY ts_utc DESC LIMIT 1
    """, (city,)).fetchone()
    return row[0] if row else None


async def _fetch_prices(client: httpx.AsyncClient, markets: list[dict]) -> None:
    for m in markets:
        try:
            r = await client.get(f"{CLOB_BASE}/book",
                                 params={"token_id": m["yes_token_id"]}, timeout=5)
            if r.status_code != 200:
                continue
            data = r.json()

            def best(key, fn):
                levels = data.get(key) or []
                prices = [float(lvl["price"] if isinstance(lvl, dict) else lvl[0])
                          for lvl in levels if lvl]
                return fn(prices) if prices else None

            bid = best("bids", max)
            ask = best("asks", min)
            if bid and ask:
                m["yes_price"] = round((bid + ask) / 2, 4)
            elif ask:
                m["yes_price"] = ask
            elif bid:
                m["yes_price"] = bid
            else:
                m["yes_price"] = None
        except Exception:
            m["yes_price"] = None
        await asyncio.sleep(0.05)


def _check_neg_risk(markets: list[dict], conn: sqlite3.Connection) -> list[dict]:
    gaps = []
    ts = dt.datetime.now(dt.timezone.utc).isoformat()

    cities = set(m["city"] for m in markets)
    for city in sorted(cities):
        city_mkts = [m for m in markets if m["city"] == city and m["yes_price"] is not None]

        above  = {m["temp_c"]: m for m in city_mkts if m["bucket_type"] == "above_eq"}
        exacts = sorted([m for m in city_mkts if m["bucket_type"] == "exact"], key=lambda m: m["temp_c"])

        # Neg-risk check
        for strike, above_mkt in above.items():
            constituent_sum = sum(m["yes_price"] for m in exacts if m["temp_c"] >= strike)
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
            # If current observed temp > highest-priced "above" strike still trading at >5¢
            for m in exacts:
                if m["yes_price"] is not None and m["yes_price"] > 0.05:
                    if obs_temp > m["temp_c"] and m["bucket_type"] == "exact":
                        log.info("🌡️  OBS  %-12s  current=%.1f°C > bucket=%.0f°C still at YES=%.3f",
                                 city, obs_temp, m["temp_c"], m["yes_price"])
                        conn.execute("""
                            INSERT INTO alerts (ts_utc, city, alert_type, detail_json)
                            VALUES (?,?,?,?)
                        """, (ts, city, "obs_mismatch", json.dumps({
                            "ts": ts, "city": city,
                            "obs_temp": obs_temp, "bucket_temp": m["temp_c"],
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
    async with httpx.AsyncClient() as client:
        await _fetch_prices(client, markets)

    priced = [m for m in markets if m.get("yes_price") is not None]
    log.info("Priced %d / %d", len(priced), len(markets))

    return _check_neg_risk(markets, conn)


async def loop(interval_s: int = 60) -> None:
    conn = sqlite3.connect(DB_PATH)
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
    conn.row_factory = sqlite3.Row

    if args.loop:
        asyncio.run(loop(args.interval))
    else:
        gaps = asyncio.run(run_once(conn, args.date))
        print(f"\n{len(gaps)} gap(s) above threshold ({GAP_THRESHOLD})")
    conn.close()
