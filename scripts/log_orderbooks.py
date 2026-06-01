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
import json
import logging
import os
import sqlite3
import sys

import httpx

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ob_logger")

REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH    = os.path.join(REPO_ROOT, "weather.db")
CLOB_BASE  = "https://clob.polymarket.com"


def _load_active_markets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT
            condition_id,
            city,
            station,
            settlement_date,
            bucket_type,
            lower_temp,
            upper_temp,
            bucket_unit,
            yes_token_id,
            no_token_id,
            close_time_utc
        FROM weather_markets
        WHERE active=1
          AND yes_token_id IS NOT NULL
          AND no_token_id IS NOT NULL
          AND settlement_date >= date('now')
        ORDER BY city, lower_temp, upper_temp
    """).fetchall()
    return [dict(r) for r in rows]


def _parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _best_level(data: dict, side: str, fn) -> tuple[float | None, float | None]:
    levels = data.get(side) or []
    parsed = []
    for level in levels:
        try:
            price = float(level["price"] if isinstance(level, dict) else level[0])
            size = float(level["size"] if isinstance(level, dict) else level[1])
        except Exception:
            continue
        parsed.append((price, size))
    if not parsed:
        return None, None
    best_price = fn(price for price, _ in parsed)
    best_size = sum(size for price, size in parsed if price == best_price)
    return best_price, best_size


async def _fetch_book(client: httpx.AsyncClient, token_id: str, sem: asyncio.Semaphore) -> dict | None:
    for attempt in range(3):
        try:
            async with sem:
                r = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in {404, 410}:
                return None
        except (httpx.TimeoutException, httpx.TransportError):
            pass
        await asyncio.sleep(0.25 * (attempt + 1))
    return None


def _snapshot_label(hours_to_close: float | None) -> str | None:
    if hours_to_close is None:
        return None
    if hours_to_close < 0:
        return "post_close"
    thresholds = [
        (0.5, "T-30m"),
        (1.0, "T-1h"),
        (2.0, "T-2h"),
        (4.0, "T-4h"),
        (8.0, "T-8h"),
        (12.0, "T-12h"),
        (24.0, "T-24h"),
    ]
    for threshold, label in thresholds:
        if hours_to_close <= threshold:
            return label
    return "open_window"


async def _snapshot_market(client: httpx.AsyncClient, sem: asyncio.Semaphore, market: dict, ts: str) -> dict | None:
    try:
        yes_book, no_book = await asyncio.gather(
            _fetch_book(client, market["yes_token_id"], sem),
            _fetch_book(client, market["no_token_id"], sem),
        )
        if not yes_book and not no_book:
            return None

        yes_bid, yes_bid_size = _best_level(yes_book or {}, "bids", max)
        yes_ask, yes_ask_size = _best_level(yes_book or {}, "asks", min)
        no_bid, no_bid_size = _best_level(no_book or {}, "bids", max)
        no_ask, no_ask_size = _best_level(no_book or {}, "asks", min)
        yes_mid = round((yes_bid + yes_ask) / 2, 4) if yes_bid is not None and yes_ask is not None else (yes_ask if yes_ask is not None else yes_bid)
        spread = round(yes_ask - yes_bid, 4) if yes_bid is not None and yes_ask is not None else None

        ts_dt = _parse_utc(ts)
        close_dt = _parse_utc(market.get("close_time_utc"))
        hours_to_close = None
        if ts_dt and close_dt:
            hours_to_close = round((close_dt - ts_dt).total_seconds() / 3600, 4)

        if no_bid is None and yes_ask is not None:
            no_bid = round(1 - yes_ask, 4)
            no_bid_size = yes_ask_size
        if no_ask is None and yes_bid is not None:
            no_ask = round(1 - yes_bid, 4)
            no_ask_size = yes_bid_size

        return {
            "condition_id": market["condition_id"],
            "ts_utc":       ts,
            "yes_bid":      yes_bid,
            "yes_ask":      yes_ask,
            "yes_bid_size": yes_bid_size,
            "yes_ask_size": yes_ask_size,
            "no_bid":       no_bid,
            "no_ask":       no_ask,
            "no_bid_size":  no_bid_size,
            "no_ask_size":  no_ask_size,
            "yes_mid":      yes_mid,
            "spread":       spread,
            "raw_book_json": json.dumps({"yes": yes_book, "no": no_book}, sort_keys=True),
            "hours_to_close": hours_to_close,
            "snapshot_label": _snapshot_label(hours_to_close),
        }
    except Exception as exc:
        log.warning("Failed snapshot for %s: %s: %s", market.get("condition_id"), type(exc).__name__, exc)
        return None


async def snapshot_all(conn: sqlite3.Connection) -> int:
    markets = _load_active_markets(conn)
    if not markets:
        log.warning("No active markets in DB — run discover_markets.py first")
        return 0

    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    saved = 0

    limits = httpx.Limits(max_connections=30, max_keepalive_connections=15)
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [_snapshot_market(client, sem, m, ts) for m in markets]
        results = await asyncio.gather(*tasks)

    for snap in results:
        if snap:
            conn.execute("""
                INSERT INTO ob_snapshots
                    (
                        condition_id, ts_utc,
                        yes_bid, yes_ask, yes_bid_size, yes_ask_size,
                        no_bid, no_ask, no_bid_size, no_ask_size,
                        yes_mid, spread, raw_book_json, hours_to_close, snapshot_label
                    )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (snap["condition_id"], snap["ts_utc"],
                  snap["yes_bid"], snap["yes_ask"],
                  snap["yes_bid_size"], snap["yes_ask_size"],
                  snap["no_bid"], snap["no_ask"],
                  snap["no_bid_size"], snap["no_ask_size"],
                  snap["yes_mid"], snap["spread"], snap["raw_book_json"],
                  snap["hours_to_close"], snap["snapshot_label"]))
            saved += 1

    conn.commit()
    return saved


async def loop(interval_s: int = 120) -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
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
        init_db(conn)
        conn.row_factory = sqlite3.Row
        n = asyncio.run(snapshot_all(conn))
        print(f"Snapshotted {n} markets")
        conn.close()
    else:
        asyncio.run(loop(args.interval))
