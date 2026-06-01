"""
neg_risk_scanner.py
===================
Checks temperature markets for neg-risk pricing inconsistencies and
observation mismatches.

Gap checks (both directions, executable prices, 2¢ threshold):
  forward_gap = above_eq YES bid  - sum(constituent bucket YES asks)  > 2¢
  reverse_gap = sum(constituent YES bids) - above_eq YES ask          > 2¢

Obs mismatch:
  METAR daily high (converted to bucket unit) invalidates a bucket still priced > 5¢

Usage:
    python scripts/neg_risk_scanner.py          # single scan today
    python scripts/neg_risk_scanner.py --loop   # continuous every 60s
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

REPO_ROOT        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH          = os.path.join(REPO_ROOT, "weather.db")
GAP_THRESHOLD    = 0.02    # 2¢ on executable prices (accounts for ~1¢ spread per leg)
OBS_MISMATCH_THR = 0.05    # 5¢ — below this YES price is effectively already priced in


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_markets(conn: sqlite3.Connection, settlement_date: str) -> list[dict]:
    """Load active markets, excluding NULL neg_risk_market_id and unknown bucket types."""
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
          AND neg_risk_market_id IS NOT NULL
          AND bucket_type != 'unknown'
        ORDER BY city, lower_temp, upper_temp
    """, (settlement_date,)).fetchall()
    return [dict(r) for r in rows]


def _daily_high_c(conn: sqlite3.Connection, city: str, settlement_date: str) -> float | None:
    """Compute daily_high_c as query-time aggregate — do not rely on stored column."""
    row = conn.execute("""
        SELECT MAX(temp_c)
        FROM wx_observations
        WHERE city=? AND local_date=?
    """, (city, settlement_date)).fetchone()
    return row[0] if row and row[0] is not None else None


def _attach_latest_prices(conn: sqlite3.Connection, markets: list[dict]) -> None:
    """Attach latest bid, ask, and sizes from ob_snapshots to each market dict."""
    for market in markets:
        row = conn.execute("""
            SELECT yes_bid, yes_ask, yes_bid_size, yes_ask_size, ts_utc
            FROM ob_snapshots
            WHERE condition_id=?
            ORDER BY ts_utc DESC LIMIT 1
        """, (market["condition_id"],)).fetchone()
        if row:
            market["yes_bid"]      = row["yes_bid"]
            market["yes_ask"]      = row["yes_ask"]
            market["yes_bid_size"] = row["yes_bid_size"]
            market["yes_ask_size"] = row["yes_ask_size"]
            market["price_ts_utc"] = row["ts_utc"]
        else:
            market["yes_bid"] = market["yes_ask"] = None
            market["yes_bid_size"] = market["yes_ask_size"] = None
            market["price_ts_utc"] = None


def _parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _c_to_bucket_unit(temp_c: float, unit: str) -> float:
    """Convert Celsius temperature to bucket unit, applying WU-equivalent rounding for F."""
    if unit == "F":
        return round(temp_c * 9 / 5 + 32)   # whole-degree F, matches WU rounding
    return temp_c


def _bucket_impossible_after_high(market: dict, high_in_bucket_unit: float) -> bool:
    """True if the current daily high makes this bucket logically impossible."""
    kind  = market["bucket_type"]
    upper = market["upper_temp"]
    if kind in {"exact", "range", "below_eq"} and upper is not None:
        return high_in_bucket_unit > upper
    return False


def _bucket_label(m: dict) -> str:
    u = m["bucket_unit"]
    if m["bucket_type"] == "range":
        return f"{m['lower_temp']:.0f}-{m['upper_temp']:.0f}{u}"
    if m["bucket_type"] == "above_eq":
        return f">={m['lower_temp']:.0f}{u}"
    if m["bucket_type"] == "below_eq":
        return f"<={m['upper_temp']:.0f}{u}"
    return f"{m['lower_temp']:.0f}{u}"


def _upsert_alert(conn: sqlite3.Connection, city: str, settlement_date: str,
                  alert_type: str, detail: dict) -> None:
    """Deduplicated alert insert — updates last_seen_utc on existing open alert."""
    now = _now()
    existing = conn.execute("""
        SELECT id FROM alerts
        WHERE city=? AND alert_type=? AND settlement_date=? AND status='open'
    """, (city, alert_type, settlement_date)).fetchone()
    if existing:
        conn.execute("""
            UPDATE alerts SET last_seen_utc=?, detail_json=? WHERE id=?
        """, (now, json.dumps(detail), existing[0]))
    else:
        conn.execute("""
            INSERT INTO alerts(opened_utc,last_seen_utc,city,settlement_date,
                               alert_type,status,detail_json)
            VALUES(?,?,?,?,?,?,?)
        """, (now, now, city, settlement_date, alert_type, "open", json.dumps(detail)))


def _close_stale_alerts(conn: sqlite3.Connection, city: str, settlement_date: str,
                         alert_type: str) -> None:
    """Close open alerts of this type when the condition no longer holds."""
    conn.execute("""
        UPDATE alerts SET status='closed', closed_utc=?
        WHERE city=? AND settlement_date=? AND alert_type=? AND status='open'
    """, (_now(), city, settlement_date, alert_type))


def _check_neg_risk(
    markets: list[dict],
    conn: sqlite3.Connection,
    settlement_date: str,
) -> list[dict]:
    now    = dt.datetime.now(dt.timezone.utc)
    gaps   = []

    cities = sorted({m["city"] for m in markets})
    for city in cities:
        city_mkts = [m for m in markets if m["city"] == city]

        # Check for multiple neg_risk_market_ids (cross-group gap possible)
        group_ids = {m["neg_risk_market_id"] for m in city_mkts if m["neg_risk_market_id"]}
        if len(group_ids) > 1:
            log.warning("Multiple neg_risk_market_ids for %s %s: %s — running cross-group check",
                        city, settlement_date, group_ids)

        above_mkts = {m["lower_temp"]: m for m in city_mkts
                      if m["bucket_type"] == "above_eq" and m["yes_bid"] is not None}
        finite_mkts = sorted(
            [m for m in city_mkts if m["bucket_type"] in {"exact", "range"}],
            key=lambda m: (m["lower_temp"] or -999, m["upper_temp"] or -999),
        )

        if not above_mkts:
            _upsert_alert(conn, city, settlement_date, "scanner_no_above_eq_bucket",
                          {"city": city, "note": "No above_eq bucket found; staircase check skipped"})
        else:
            # Close stale no-above_eq alerts if above buckets now exist
            _close_stale_alerts(conn, city, settlement_date, "scanner_no_above_eq_bucket")

        for strike, above_mkt in above_mkts.items():
            above_bid = above_mkt.get("yes_bid")
            above_ask = above_mkt.get("yes_ask")
            if above_bid is None or above_ask is None:
                continue

            constituents = [m for m in finite_mkts
                            if m["lower_temp"] is not None and m["lower_temp"] >= strike
                            and m["yes_bid"] is not None and m["yes_ask"] is not None]
            if not constituents:
                continue

            sum_ask = sum(m["yes_ask"] for m in constituents)
            sum_bid = sum(m["yes_bid"] for m in constituents)

            # Forward gap: above_eq is cheap vs sum (buy constituents, sell above)
            forward_gap = above_bid - sum_ask
            if forward_gap > GAP_THRESHOLD:
                label = f"🚨 FWD  {city} >={strike:.0f} bid={above_bid:.4f} sum_ask={sum_ask:.4f} gap={forward_gap:+.4f}"
                log.info(label)
                detail = {"city": city, "strike": strike, "above_bid": above_bid,
                          "sum_ask": round(sum_ask, 4), "forward_gap": round(forward_gap, 4)}
                _upsert_alert(conn, city, settlement_date, "neg_risk_gap_forward", detail)
                gaps.append(detail)
            else:
                _close_stale_alerts(conn, city, settlement_date, "neg_risk_gap_forward")

            # Reverse gap: staircase sum is cheap vs above_eq (buy above, sell constituents)
            reverse_gap = sum_bid - above_ask
            if reverse_gap > GAP_THRESHOLD:
                label = f"🚨 REV  {city} >={strike:.0f} ask={above_ask:.4f} sum_bid={sum_bid:.4f} gap={reverse_gap:+.4f}"
                log.info(label)
                detail = {"city": city, "strike": strike, "above_ask": above_ask,
                          "sum_bid": round(sum_bid, 4), "reverse_gap": round(reverse_gap, 4)}
                _upsert_alert(conn, city, settlement_date, "neg_risk_gap_reverse", detail)
                gaps.append(detail)
            else:
                _close_stale_alerts(conn, city, settlement_date, "neg_risk_gap_reverse")

        # Observation mismatch check
        obs_c = _daily_high_c(conn, city, settlement_date)
        if obs_c is not None and city_mkts:
            bucket_unit = city_mkts[0]["bucket_unit"]
            high = _c_to_bucket_unit(obs_c, bucket_unit)   # C→F with round() for F markets

            for m in city_mkts:
                close_dt = _parse_utc(m.get("close_time_utc"))
                if close_dt and now >= close_dt:
                    continue    # Type C post-close: no obs_mismatch alerts
                yes_ask = m.get("yes_ask")
                if yes_ask is not None and yes_ask > OBS_MISMATCH_THR:
                    if _bucket_impossible_after_high(m, high):
                        log.info("OBS  %-12s  high=%.1f%s invalidates %-10s YES ask=%.3f",
                                 city, high, bucket_unit, _bucket_label(m), yes_ask)
                        _upsert_alert(conn, city, settlement_date, "obs_mismatch", {
                            "city": city, "obs_temp_c": obs_c,
                            "high_in_bucket_unit": high, "bucket_unit": bucket_unit,
                            "bucket": _bucket_label(m), "yes_ask": yes_ask,
                        })

    conn.commit()
    return gaps


async def run_once(conn: sqlite3.Connection, settlement_date: str) -> list[dict]:
    markets = _load_markets(conn, settlement_date)
    if not markets:
        log.warning("No markets for %s — run discover_markets.py first", settlement_date)
        return []

    log.info("Scanning %d markets for %s", len(markets), settlement_date)
    _attach_latest_prices(conn, markets)
    priced = sum(1 for m in markets if m.get("yes_bid") is not None)
    log.info("Priced %d / %d", priced, len(markets))

    return _check_neg_risk(markets, conn, settlement_date)


async def loop(interval_s: int = 60) -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row
    log.info("Starting neg-risk scanner | interval=%ds | threshold=%.3f", interval_s, GAP_THRESHOLD)
    while True:
        try:
            today = dt.datetime.now(dt.timezone.utc).date().isoformat()
            gaps  = await run_once(conn, today)
            if gaps:
                log.info("⚡ %d gap(s) found", len(gaps))
        except Exception as e:
            log.exception("scan error: %s", e)
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",     action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--date",     default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row

    if args.loop:
        asyncio.run(loop(args.interval))
    else:
        gaps = asyncio.run(run_once(conn, args.date))
        print(f"\n{len(gaps)} gap(s) above {GAP_THRESHOLD}")
    conn.close()
