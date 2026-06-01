"""
log_orderbooks.py
=================
Polls the Polymarket CLOB every 2 minutes for all active weather markets
and stores YES/NO bid-ask snapshots in weather.db.

Usage:
    cd /Users/eric/polymarket-weather
    .venv/bin/python scripts/log_orderbooks.py [--interval 120]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import sqlite3

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ob_logger")

REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH    = os.path.join(REPO_ROOT, "weather.db")
CLOB_BASE  = "https://clob.polymarket.com"


def _load_active_markets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT condition_id, city, station, settlement_date, temp_c, yes_token_id
        FROM weather_markets
        WHERE active=1 AND yes_token_id IS NOT NULL
          AND settlement_date >= date('now')
        ORDER BY city, temp_c
    """).fetchall()
    return [dict(r) for r in rows]


async def _snapshot_market(client: httpx.AsyncClient, market: dict, ts: str) -> dict | None:
    try:
        r = await client.get(
            f"{CLOB_BASE}/book",
            params={"token_id": market["yes_token_id"]},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        data = r.json()

        def best_price(key, fn):
            levels = data.get(key) or []
            prices = []
            for lvl in levels:
                try:
                    p = float(lvl["price"] if isinstance(lvl, dict) else lvl[0])
                    prices.append(p)
                except Exception:
                    pass
            return fn(prices) if prices else None

        yes_bid = best_price("bids", max)
        yes_ask = best_price("asks", min)
        yes_mid = round((yes_bid + yes_ask) / 2, 4) if yes_bid and yes_ask else (yes_ask or yes_bid)

        return {
            "condition_id": market["condition_id"],
            "ts_utc":       ts,
            "yes_bid":      yes_bid,
            "yes_ask":      yes_ask,
            "no_bid":       round(1 - yes_ask, 4) if yes_ask else None,
            "no_ask":       round(1 - yes_bid, 4) if yes_bid else None,
            "yes_mid":      yes_mid,
        }
    except Exception:
        return None


async def snapshot_all(conn: sqlite3.Connection) -> int:
    markets = _load_active_markets(conn)
    if not markets:
        log.warning("No active markets in DB — run discover_markets.py first")
        return 0

    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    saved = 0

    async with httpx.AsyncClient() as client:
        tasks = [_snapshot_market(client, m, ts) for m in markets]
        results = await asyncio.gather(*tasks)

    for snap in results:
        if snap:
            conn.execute("""
                INSERT INTO ob_snapshots
                    (condition_id, ts_utc, yes_bid, yes_ask, no_bid, no_ask, yes_mid)
                VALUES (?,?,?,?,?,?,?)
            """, (snap["condition_id"], snap["ts_utc"],
                  snap["yes_bid"], snap["yes_ask"],
                  snap["no_bid"], snap["no_ask"], snap["yes_mid"]))
            saved += 1

    conn.commit()
    return saved


async def loop(interval_s: int = 120) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    log.info("Starting orderbook logger | interval=%ds", interval_s)

    while True:
        try:
            n = await snapshot_all(conn)
            log.info("Snapshotted %d markets", n)
        except Exception as e:
            log.exception("snapshot error: %s", e)
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        n = asyncio.run(snapshot_all(conn))
        print(f"Snapshotted {n} markets")
        conn.close()
    else:
        asyncio.run(loop(args.interval))
