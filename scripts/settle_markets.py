"""
Apply source-of-record proxy values to weather buckets.

This script does not claim final Polymarket/UMA settlement. It computes the proxy
YES/NO outcome using `settlement_value_proxy` already fetched from the market's
named source adapter, then stores `proxy_outcome` on each bucket market.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from init_db import init_db

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "weather.db")


def _round_for_market(value: float, rule: str | None) -> float:
    # Polymarket exact/range weather rules generally use the displayed whole degree.
    # HKO can publish one decimal; keep one-decimal sources as-is until the market
    # rules explicitly require whole-degree rounding.
    if rule in {"whole_degree", "round"}:
        return float(round(value))
    return value


def _proxy_outcome(row: sqlite3.Row) -> str | None:
    value = row["settlement_value_proxy"]
    if value is None:
        return None
    value = _round_for_market(float(value), row["settlement_rounding_rule"])
    kind = row["bucket_type"]
    lower = row["lower_temp"]
    upper = row["upper_temp"]
    if kind == "exact" and lower is not None:
        return "YES" if value == float(lower) else "NO"
    if kind == "range" and lower is not None and upper is not None:
        return "YES" if float(lower) <= value <= float(upper) else "NO"
    if kind == "above_eq" and lower is not None:
        return "YES" if value >= float(lower) else "NO"
    if kind == "below_eq" and upper is not None:
        return "YES" if value <= float(upper) else "NO"
    return None


def apply_proxy_outcomes(conn: sqlite3.Connection, settlement_date: str | None = None) -> int:
    params: list[str] = []
    where = "WHERE settlement_value_proxy IS NOT NULL"
    if settlement_date:
        where += " AND settlement_date=?"
        params.append(settlement_date)

    rows = conn.execute(f"""
        SELECT
            condition_id, bucket_type, lower_temp, upper_temp,
            settlement_value_proxy, settlement_rounding_rule
        FROM weather_markets
        {where}
    """, params).fetchall()

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    updated = 0
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
    conn.commit()
    return updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Settlement local date, YYYY-MM-DD")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.row_factory = sqlite3.Row
    count = apply_proxy_outcomes(conn, args.date)
    print(f"Applied proxy outcomes to {count} market(s)")
    conn.close()
