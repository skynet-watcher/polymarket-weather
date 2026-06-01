"""
settle_markets.py
=================
Apply source-of-record proxy values to weather buckets and write proxy_outcome.

Runs automatically via polling trigger: checks every 15 minutes for markets
whose local day has ended (temp_window_start_utc + 26h < now) and whose
settlement_value_proxy has not yet been written.

Also validates that each city+date has exactly one YES outcome after settlement
and flags integrity errors.

Usage:
    python scripts/settle_markets.py --loop      # continuous trigger (recommended)
    python scripts/settle_markets.py --date 2026-06-01   # settle one date manually
    python scripts/settle_markets.py --once      # settle all eligible markets now
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("settle")

REPO_ROOT            = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH              = os.path.join(REPO_ROOT, "weather.db")
POLL_INTERVAL_S      = 900   # 15 minutes
FINALIZATION_HOURS   = 26    # temp_window_start + 26h < now → local day complete + source finalised


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _round_for_market(value: float, rule: str | None) -> float:
    """Apply the resolution rounding operation to the source value."""
    if rule in {"round", "whole_degree", "one_decimal"}:
        return float(round(value))
    if rule == "floor":
        import math
        return float(math.floor(value))
    if rule == "ceiling":
        import math
        return float(math.ceil(value))
    return value   # 'unknown' — return as-is; resolution_status stays proxy_only


def _proxy_outcome(row: sqlite3.Row) -> str | None:
    value = row["settlement_value_proxy"]
    if value is None:
        return None
    if row["bucket_type"] == "unknown" or row["resolution_status"] == "cancelled":
        return None    # skip unparseable and cancelled markets
    value = _round_for_market(float(value), row["settlement_rounding_rule"])
    kind  = row["bucket_type"]
    lower = row["lower_temp"]
    upper = row["upper_temp"]
    if kind == "exact"    and lower is not None:
        return "YES" if value == float(lower) else "NO"
    if kind == "range"    and lower is not None and upper is not None:
        return "YES" if float(lower) <= value <= float(upper) else "NO"
    if kind == "above_eq" and lower is not None:
        return "YES" if value >= float(lower) else "NO"
    if kind == "below_eq" and upper is not None:
        return "YES" if value <= float(upper) else "NO"
    return None


def _eligible_city_dates(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return (city, settlement_date) pairs ready to settle.

    Ready means: temp_window_start_utc + FINALIZATION_HOURS < now
    and settlement_value_proxy has been written by fetch_settlement_sources.py
    and proxy_outcome has not yet been assigned.
    """
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(hours=FINALIZATION_HOURS)).isoformat()
    rows = conn.execute("""
        SELECT DISTINCT city, settlement_date
        FROM weather_markets
        WHERE settlement_value_proxy IS NOT NULL
          AND proxy_outcome IS NULL
          AND temp_window_start_utc < ?
          AND resolution_status != 'cancelled'
        ORDER BY settlement_date, city
    """, (cutoff,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def apply_proxy_outcomes(
    conn: sqlite3.Connection,
    settlement_date: str | None = None,
) -> int:
    params: list[str] = []
    where  = """WHERE settlement_value_proxy IS NOT NULL
                  AND bucket_type != 'unknown'
                  AND (resolution_status IS NULL OR resolution_status = 'proxy_only')"""
    if settlement_date:
        where += " AND settlement_date=?"
        params.append(settlement_date)

    rows = conn.execute(f"""
        SELECT condition_id, city, settlement_date, bucket_type,
               lower_temp, upper_temp, settlement_value_proxy, settlement_rounding_rule,
               resolution_status
        FROM weather_markets {where}
    """, params).fetchall()

    now     = _now()
    updated = 0
    by_city_date: dict[tuple, list] = {}

    for row in rows:
        outcome = _proxy_outcome(row)
        if outcome is None:
            continue
        conn.execute("""
            UPDATE weather_markets
            SET proxy_outcome=?,
                resolution_status=COALESCE(resolution_status, 'proxy_only'),
                settled_at_utc=COALESCE(settled_at_utc, ?)
            WHERE condition_id=?
        """, (outcome, now, row["condition_id"]))
        updated += 1
        key = (row["city"], row["settlement_date"])
        by_city_date.setdefault(key, []).append(outcome)

    conn.commit()

    # Post-condition: exactly one YES per city+date
    _validate_integrity(conn, by_city_date, settlement_date)

    return updated


def _validate_integrity(
    conn: sqlite3.Connection,
    by_city_date: dict,
    settlement_date: str | None,
) -> None:
    """Assert exactly one YES per city+date. Flag integrity errors."""
    now = _now()

    # If we just wrote outcomes, check them
    for (city, sdate), outcomes in by_city_date.items():
        yes_count = outcomes.count("YES")
        if yes_count == 1:
            continue
        log.error("Settlement integrity error: %s %s has %d YES outcomes", city, sdate, yes_count)
        conn.execute("""
            UPDATE weather_markets SET resolution_status='disputed'
            WHERE city=? AND settlement_date=? AND proxy_outcome IS NOT NULL
        """, (city, sdate))
        conn.execute("""
            INSERT INTO alerts(opened_utc,last_seen_utc,city,settlement_date,
                               alert_type,status,detail_json)
            VALUES(?,?,?,?,?,?,?)
        """, (now, now, city, sdate, "settlement_integrity_error", "open",
              json.dumps({"city": city, "settlement_date": sdate, "yes_count": yes_count})))
    conn.commit()

    # Also check any previously settled markets for the date (catches edge cases)
    date_filter = f"AND settlement_date='{settlement_date}'" if settlement_date else ""
    integrity_rows = conn.execute(f"""
        SELECT city, settlement_date, SUM(proxy_outcome='YES') as yes_count
        FROM weather_markets
        WHERE proxy_outcome IS NOT NULL
          AND bucket_type != 'unknown'
          AND resolution_status != 'cancelled'
          {date_filter}
        GROUP BY city, settlement_date
        HAVING yes_count != 1
    """).fetchall()

    for row in integrity_rows:
        city, sdate, yes_count = row[0], row[1], row[2]
        key = (city, sdate)
        if key in by_city_date:
            continue    # already handled above
        log.warning("Integrity check: %s %s has %d YES (may be pre-existing disputed)", city, sdate, yes_count)


def loop_once(conn: sqlite3.Connection) -> int:
    """Settle all eligible city+dates and return total markets updated."""
    pairs = _eligible_city_dates(conn)
    if not pairs:
        return 0
    total = 0
    for city, sdate in pairs:
        log.info("Settling %s %s ...", city, sdate)
        n = apply_proxy_outcomes(conn, sdate)
        log.info("  %s %s: %d markets settled", city, sdate, n)
        total += n
    return total


def run_loop() -> None:
    """Continuous polling trigger — runs every POLL_INTERVAL_S seconds."""
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row
    log.info("Settlement trigger running | poll=%ds | finalization=%dh",
             POLL_INTERVAL_S, FINALIZATION_HOURS)
    while True:
        try:
            n = loop_once(conn)
            if n:
                log.info("Applied %d proxy outcomes", n)
        except Exception as e:
            log.exception("settle error: %s", e)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",  help="Settle one local date YYYY-MM-DD")
    parser.add_argument("--once",  action="store_true", help="Settle all eligible now")
    parser.add_argument("--loop",  action="store_true", help="Continuous polling trigger")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row

    if args.loop:
        run_loop()
    elif args.date:
        count = apply_proxy_outcomes(conn, args.date)
        print(f"Applied proxy outcomes to {count} market(s) for {args.date}")
    else:
        count = loop_once(conn)
        print(f"Applied proxy outcomes to {count} market(s)")
    conn.close()
