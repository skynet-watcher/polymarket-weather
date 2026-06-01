"""
log_orderbooks.py
=================
Polls the Polymarket CLOB every 2 minutes for all active weather markets
and stores YES/NO bid-ask snapshots in weather.db.

Usage:
    python scripts/log_orderbooks.py            # continuous loop (default 120s)
    python scripts/log_orderbooks.py --once     # single pass
    python scripts/log_orderbooks.py --interval 60
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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH   = os.path.join(REPO_ROOT, "weather.db")
CLOB_BASE = "https://clob.polymarket.com"

# Snapshot bin thresholds (hours_to_close ≤ threshold → label).
# Must match PLAN.md Q3 standard intervals.
_LABEL_THRESHOLDS = [
    (0.5,  "T-30min"),
    (1.0,  "T-1h"),
    (3.0,  "T-3h"),
    (6.0,  "T-6h"),
    (12.0, "T-12h"),
    (24.0, "T-24h"),
]


def _load_active_markets(conn: sqlite3.Connection) -> list[dict]:
    """Load all active markets that are still within their monitoring window.

    Uses date('now', '-1 day') so Type C cities (NYC, Miami) are not dropped
    after UTC midnight when their local temperature window continues until ~04:00 UTC.
    """
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
            close_time_utc,
            first_seen_utc
        FROM weather_markets
        WHERE active=1
          AND yes_token_id IS NOT NULL
          AND no_token_id IS NOT NULL
          AND settlement_date >= date('now', '-1 day')
        ORDER BY city, lower_temp, upper_temp
    """).fetchall()
    return [dict(r) for r in rows]


def _load_open_label_set(conn: sqlite3.Connection) -> set[str]:
    """Return condition_ids that already have an 'open' snapshot label in the DB."""
    rows = conn.execute(
        "SELECT DISTINCT condition_id FROM ob_snapshots WHERE snapshot_label='open'"
    ).fetchall()
    return {r[0] for r in rows}


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
            size  = float(level["size"]  if isinstance(level, dict) else level[1])
        except Exception:
            continue
        parsed.append((price, size))
    if not parsed:
        return None, None
    best_price = fn(price for price, _ in parsed)
    best_size  = sum(size for price, size in parsed if price == best_price)
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


def _snapshot_label(
    hours_to_close: float | None,
    ts_dt: dt.datetime | None,
    market: dict,
    already_open: bool,
) -> tuple[str, bool]:
    """Return (label, open_now_assigned).

    Two kinds of labels:
      - Bin labels: every snapshot gets one based on hours_to_close.
      - 'open' label: fires ONCE per market, on the first snapshot within
        5 minutes of first_seen_utc. Never overwritten.

    Returns (label, True) when 'open' is assigned this call.
    Returns (label, False) when 'open' is not assigned (already set or window missed).
    """
    # Check open label first — takes precedence over bin label for this snapshot
    if not already_open and ts_dt is not None:
        first_seen_dt = _parse_utc(market.get("first_seen_utc"))
        if first_seen_dt and abs((ts_dt - first_seen_dt).total_seconds()) <= 300:
            return "open", True

    if hours_to_close is None:
        return "open_window", False
    if hours_to_close < 0:
        return "post_close", False
    for threshold, label in _LABEL_THRESHOLDS:
        if hours_to_close <= threshold:
            return label, False
    return "open_window", False


async def _snapshot_market(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    market: dict,
    ts: str,
    already_open: bool,
) -> dict:
    """Fetch and return a snapshot row for one market.

    Always returns a dict — price fields are None when books are empty.
    This preserves the "we polled at T and books were empty" record for
    post-close convergence analysis.
    """
    ts_dt = _parse_utc(ts)
    close_dt = _parse_utc(market.get("close_time_utc"))
    hours_to_close: float | None = None
    if ts_dt and close_dt:
        hours_to_close = round((close_dt - ts_dt).total_seconds() / 3600, 4)

    base = {
        "condition_id":   market["condition_id"],
        "ts_utc":         ts,
        "yes_bid":        None, "yes_ask":     None,
        "yes_bid_size":   None, "yes_ask_size": None,
        "no_bid":         None, "no_ask":      None,
        "no_bid_size":    None, "no_ask_size":  None,
        "yes_mid":        None, "spread":       None,
        "raw_book_json":  "{}",
        "hours_to_close": hours_to_close,
        "open_assigned":  False,
    }

    label, open_now = _snapshot_label(hours_to_close, ts_dt, market, already_open)
    base["snapshot_label"] = label
    base["open_assigned"]  = open_now

    if not market.get("close_time_utc"):
        log.warning("close_time_utc NULL for %s — run discover_markets.py",
                    market["condition_id"][:10])

    try:
        yes_book, no_book = await asyncio.gather(
            _fetch_book(client, market["yes_token_id"], sem),
            _fetch_book(client, market["no_token_id"],  sem),
        )

        # Even if books are empty, store the row (not None) — empty book is data.
        yes_bid, yes_bid_size = _best_level(yes_book or {}, "bids", max)
        yes_ask, yes_ask_size = _best_level(yes_book or {}, "asks", min)
        no_bid,  no_bid_size  = _best_level(no_book  or {}, "bids", max)
        no_ask,  no_ask_size  = _best_level(no_book  or {}, "asks", min)

        yes_mid = (
            round((yes_bid + yes_ask) / 2, 4)
            if yes_bid is not None and yes_ask is not None
            else (yes_ask if yes_ask is not None else yes_bid)
        )
        spread = (
            round(yes_ask - yes_bid, 4)
            if yes_bid is not None and yes_ask is not None
            else None
        )

        # Derive implied NO prices from YES if NO book is empty
        if no_bid is None and yes_ask is not None:
            no_bid, no_bid_size = round(1 - yes_ask, 4), yes_ask_size
        if no_ask is None and yes_bid is not None:
            no_ask, no_ask_size = round(1 - yes_bid, 4), yes_bid_size

        base.update({
            "yes_bid": yes_bid, "yes_ask": yes_ask,
            "yes_bid_size": yes_bid_size, "yes_ask_size": yes_ask_size,
            "no_bid": no_bid,  "no_ask": no_ask,
            "no_bid_size": no_bid_size,  "no_ask_size": no_ask_size,
            "yes_mid": yes_mid, "spread": spread,
            "raw_book_json": json.dumps({"yes": yes_book, "no": no_book}, sort_keys=True),
        })

    except Exception as exc:
        log.warning("Failed fetching books for %s: %s: %s",
                    market.get("condition_id"), type(exc).__name__, exc)

    return base


async def snapshot_all(conn: sqlite3.Connection, open_label_set: set[str]) -> tuple[int, set[str]]:
    """Take one snapshot of all active markets.

    Returns (rows_saved, updated_open_label_set).
    """
    markets = _load_active_markets(conn)
    if not markets:
        log.warning("No active markets in DB — run discover_markets.py first")
        return 0, open_label_set

    ts   = dt.datetime.now(dt.timezone.utc).isoformat()
    saved = 0

    limits = httpx.Limits(max_connections=30, max_keepalive_connections=15)
    sem    = asyncio.Semaphore(12)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [
            _snapshot_market(client, sem, m, ts, m["condition_id"] in open_label_set)
            for m in markets
        ]
        results = await asyncio.gather(*tasks)

    for snap in results:
        conn.execute("""
            INSERT INTO ob_snapshots
                (condition_id, ts_utc,
                 yes_bid, yes_ask, yes_bid_size, yes_ask_size,
                 no_bid,  no_ask,  no_bid_size,  no_ask_size,
                 yes_mid, spread, raw_book_json, hours_to_close, snapshot_label)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            snap["condition_id"], snap["ts_utc"],
            snap["yes_bid"],      snap["yes_ask"],
            snap["yes_bid_size"], snap["yes_ask_size"],
            snap["no_bid"],       snap["no_ask"],
            snap["no_bid_size"],  snap["no_ask_size"],
            snap["yes_mid"],      snap["spread"],
            snap["raw_book_json"],
            snap["hours_to_close"],
            snap["snapshot_label"],
        ))
        if snap.get("open_assigned"):
            open_label_set.add(snap["condition_id"])
        saved += 1

    conn.commit()
    return saved, open_label_set


async def loop(interval_s: int = 120) -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row
    log.info("Starting orderbook logger | interval=%ds", interval_s)

    # Load existing open-label assignments so we don't re-fire them after restart
    open_label_set = _load_open_label_set(conn)
    log.info("Loaded %d existing 'open' labels from DB", len(open_label_set))

    while True:
        try:
            n, open_label_set = await snapshot_all(conn, open_label_set)
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
        open_label_set = _load_open_label_set(conn)
        n, _ = asyncio.run(snapshot_all(conn, open_label_set))
        print(f"Snapshotted {n} markets")
        conn.close()
    else:
        asyncio.run(loop(args.interval))
