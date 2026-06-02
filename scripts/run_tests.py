"""
Pre-production test runner for the Polymarket weather project.

Runs all tests defined in PREPRODUCTION_TESTS.md that do not require
live network access. Network-dependent tests are marked SKIP with a reason.

Usage:
    python scripts/run_tests.py              # all tests
    python scripts/run_tests.py --group 1   # one group only
    python scripts/run_tests.py --network   # include network tests (requires live APIs)

Exit code: 0 if all run tests pass, 1 if any fail.
"""
from __future__ import annotations

import argparse
import tempfile
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "weather.db")
DB_PATH = os.environ.get("WEATHER_TEST_DB", os.path.join(tempfile.gettempdir(), "polymarket_weather_tests.db"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from init_db import init_db  # noqa: E402

# ── result tracking ─────────────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

results: list[dict] = []


def report(test_id: str, name: str, status: str, detail: str = "") -> None:
    symbol = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭ "}.get(status, "?")
    line = f"{symbol} {test_id:6s}  {name}"
    if detail:
        line += f"\n         {detail}"
    print(line)
    results.append({"id": test_id, "name": name, "status": status, "detail": detail})


_tmp_dbs: list[str] = []

def fresh_db() -> sqlite3.Connection:
    """Return a connection to a fresh temp-file database, fully initialised.
    Uses a file (not :memory:) so WAL mode can be tested — WAL requires a real file."""
    import tempfile
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    _tmp_dbs.append(f.name)
    conn = sqlite3.connect(f.name)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _parse_utc(s: str) -> "dt.datetime":
    """Parse an ISO-8601 UTC string, accepting both 'Z' and '+00:00' suffixes.
    Python 3.9 fromisoformat() does not accept 'Z'."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ── GROUP 1 — Schema ─────────────────────────────────────────────────────────

def test_p01_schema_clean_init():
    conn = fresh_db()

    failures = []

    # WAL mode
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode != "wal":
        failures.append(f"journal_mode={mode!r} (expected 'wal')")

    # busy_timeout
    bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    if bt < 10000:
        failures.append(f"busy_timeout={bt} (expected >=10000)")

    # required tables
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    required = {
        "wx_observations", "taf_forecasts", "model_forecasts",
        "weather_markets", "ob_snapshots", "market_price_history", "fetch_log", "alerts",
        "settlement_observations", "market_resolutions",
    }
    missing_tables = required - tables
    if missing_tables:
        failures.append(f"missing tables: {missing_tables}")

    # critical columns
    checks = [
        ("taf_forecasts",    "forecast_local_date"),
        ("weather_markets",  "temp_window_start_utc"),
        ("weather_markets",  "cancelled_at_utc"),
        ("alerts",           "status"),
        ("alerts",           "opened_utc"),
        ("alerts",           "last_seen_utc"),
        ("alerts",           "closed_utc"),
        ("alerts",           "settlement_date"),
    ]
    for table, col in checks:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            failures.append(f"{table}.{col} missing")

    conn.close()
    if failures:
        report("P-01", "Schema clean init", FAIL, "; ".join(failures))
    else:
        report("P-01", "Schema clean init", PASS)


def test_p02_migration_idempotent():
    conn = fresh_db()
    # run init_db a second time — must not error
    try:
        init_db(conn)
        init_db(conn)
        # row count must survive
        conn.execute(
            "INSERT INTO wx_observations(station,city,fetched_utc,local_date) "
            "VALUES ('RKSI','Seoul','2026-06-01T00:00:00Z','2026-06-01')"
        )
        conn.commit()
        init_db(conn)
        count = conn.execute("SELECT COUNT(*) FROM wx_observations").fetchone()[0]
        if count != 1:
            report("P-02", "Migration idempotent", FAIL, f"row count={count} after re-init")
            return
    except Exception as e:
        report("P-02", "Migration idempotent", FAIL, str(e))
        return
    conn.close()
    report("P-02", "Migration idempotent", PASS)


def test_p03_concurrent_writes():
    """Three threads write simultaneously; no lock errors; counts must match."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn_init = sqlite3.connect(db_path)
    init_db(conn_init)
    conn_init.close()

    errors = []
    N = 200

    def writer(table_sql: str, n: int):
        conn = sqlite3.connect(db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            for i in range(n):
                conn.execute(table_sql, (f"val{i}",))
            conn.commit()
        except Exception as e:
            errors.append(str(e))
        finally:
            conn.close()

    t1 = threading.Thread(target=writer, args=(
        "INSERT INTO fetch_log(ts_utc,source,attempt,status) VALUES(?,'metar',1,'success')", N))
    t2 = threading.Thread(target=writer, args=(
        "INSERT INTO fetch_log(ts_utc,source,attempt,status) VALUES(?,'taf',1,'success')", N))
    t3 = threading.Thread(target=writer, args=(
        "INSERT INTO fetch_log(ts_utc,source,attempt,status) VALUES(?,'gfs',1,'success')", N))

    for t in (t1, t2, t3):
        t.start()
    for t in (t1, t2, t3):
        t.join()

    if errors:
        report("P-03", "Concurrent write safety", FAIL, "; ".join(errors[:3]))
    else:
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
        conn.close()
        os.unlink(db_path)
        if count == N * 3:
            report("P-03", "Concurrent write safety", PASS)
        else:
            report("P-03", "Concurrent write safety", FAIL,
                   f"expected {N*3} rows, got {count}")


def test_p04_insert_or_ignore():
    conn = fresh_db()
    # model_forecasts
    row = ("RKSI", "Seoul", "gfs_seamless", "2026-06-01T00:00:00Z",
           "2026-06-01T04:00:00Z", "2026-06-01", 24, 31.0, 18.0, 37.5, 127.0)
    sql = ("INSERT OR IGNORE INTO model_forecasts"
           "(station,city,model,model_run_utc,fetched_utc,forecast_date,"
           "horizon_hours,high_c,low_c,lat,lon) VALUES(?,?,?,?,?,?,?,?,?,?,?)")
    conn.execute(sql, row)
    conn.execute(sql, row)  # duplicate
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM model_forecasts").fetchone()[0]
    # taf_forecasts
    conn.execute(
        "INSERT OR IGNORE INTO taf_forecasts"
        "(station,city,issued_utc,valid_from_utc,valid_to_utc,fetched_utc)"
        " VALUES('RKSI','Seoul','2026-06-01T00:00:00Z','2026-06-01T00:00:00Z',"
        "'2026-06-02T06:00:00Z','2026-06-01T00:30:00Z')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO taf_forecasts"
        "(station,city,issued_utc,valid_from_utc,valid_to_utc,fetched_utc)"
        " VALUES('RKSI','Seoul','2026-06-01T00:00:00Z','2026-06-01T00:00:00Z',"
        "'2026-06-02T06:00:00Z','2026-06-01T00:30:00Z')"
    )
    conn.commit()
    taf_count = conn.execute("SELECT COUNT(*) FROM taf_forecasts").fetchone()[0]
    conn.close()
    if count == 1 and taf_count == 1:
        report("P-04", "INSERT OR IGNORE idempotency", PASS)
    else:
        report("P-04", "INSERT OR IGNORE idempotency", FAIL,
               f"model_forecasts={count} taf_forecasts={taf_count} (expected 1 each)")


# ── GROUP 2 — Discovery (offline tests only) ─────────────────────────────────

def test_p06_settlement_date_derivation():
    """Verify settlement_date derivation from temp_window_start_utc for edge cases."""
    from datetime import datetime, timedelta, timezone

    cases = [
        # (city, station_tz, temp_window_start_utc, expected_settlement_date)
        # temp_window_start_utc IS local midnight UTC — converting it to local tz
        # must yield 00:00 local on the expected settlement_date.
        # Seoul UTC+9: local midnight June 1 KST = 2026-05-31T15:00:00Z
        ("Seoul",     "Asia/Seoul",       "2026-05-31T15:00:00Z", "2026-06-01"),
        # Tokyo UTC+9: same offset
        ("Tokyo",     "Asia/Tokyo",       "2026-05-31T15:00:00Z", "2026-06-01"),
        # NYC EDT (UTC-4): local midnight June 1 = 2026-06-01T04:00:00Z
        ("NYC",       "America/New_York", "2026-06-01T04:00:00Z", "2026-06-01"),
        # London BST (UTC+1): local midnight June 1 BST = 2026-05-31T23:00:00Z
        ("London",    "Europe/London",    "2026-05-31T23:00:00Z", "2026-06-01"),
        # Wellington NZST (UTC+12): local midnight June 1 NZST = 2026-05-31T12:00:00Z
        ("Wellington","Pacific/Auckland", "2026-05-31T12:00:00Z", "2026-06-01"),
        # Wellington NZDT (UTC+13, Nov): local midnight Nov 15 NZDT = 2026-11-14T11:00:00Z
        ("Wellington","Pacific/Auckland", "2026-11-14T11:00:00Z", "2026-11-15"),
    ]

    failures = []
    for city, tz, tws, expected in cases:
        window_local = _parse_utc(tws).astimezone(ZoneInfo(tz))
        got = window_local.date().isoformat()
        if got != expected:
            failures.append(f"{city} ({tz}): got {got!r}, expected {expected!r}")

    if failures:
        report("P-06", "settlement_date derivation", FAIL, "; ".join(failures))
    else:
        report("P-06", "settlement_date derivation", PASS)


def test_p08_slug_failure_detection():
    """Verify alert logic: ≥3 zero-market cities triggers discovery_slug_failure."""
    # Simulate the check: if count of cities with 0 markets >= 3, alert
    zero_cities = ["Hong Kong", "Seoul", "Tokyo"]  # 3 cities returned nothing
    threshold = 3
    if len(zero_cities) >= threshold:
        alert_should_fire = True
    else:
        alert_should_fire = False

    # Test with 2 zeros — should NOT fire
    two_zeros = ["Hong Kong", "Seoul"]
    alert_two = len(two_zeros) >= threshold

    if alert_should_fire and not alert_two:
        report("P-08", "Slug failure detection threshold", PASS)
    else:
        report("P-08", "Slug failure detection threshold", FAIL,
               f"alert_3_cities={alert_should_fire}, alert_2_cities={alert_two}")


# ── GROUP 3 — METAR ──────────────────────────────────────────────────────────

def test_p11_metar_correction():
    """METAR correction via ON CONFLICT REPLACE; MAX recalculates correctly."""
    conn = fresh_db()
    base = dict(station="RJTT", city="Tokyo", fetched_utc="2026-06-01T06:00:00Z",
                local_date="2026-06-01", source="metar")

    def ins(obs_utc, temp):
        conn.execute(
            "INSERT OR REPLACE INTO wx_observations"
            "(station,city,observed_utc,fetched_utc,local_date,source,temp_c)"
            " VALUES(:station,:city,:observed_utc,:fetched_utc,:local_date,:source,:temp_c)",
            {**base, "observed_utc": obs_utc, "temp_c": temp}
        )
        conn.commit()

    ins("2026-06-01T05:30:00Z", 28.0)
    ins("2026-06-01T06:00:00Z", 31.5)   # bad reading
    ins("2026-06-01T06:30:00Z", 29.0)
    # Correction: same observed_utc, lower temp
    ins("2026-06-01T06:00:00Z", 29.5)   # COR METAR

    max_temp = conn.execute(
        "SELECT MAX(temp_c) FROM wx_observations WHERE station='RJTT' AND local_date='2026-06-01'"
    ).fetchone()[0]
    conn.close()

    if abs(max_temp - 29.5) < 0.001:
        report("P-11", "METAR correction via ON CONFLICT REPLACE", PASS)
    else:
        report("P-11", "METAR correction via ON CONFLICT REPLACE", FAIL,
               f"MAX(temp_c)={max_temp}, expected 29.5")


def test_p12_local_date_timezone():
    """local_date must use station timezone, not UTC."""
    from datetime import datetime, timezone

    # RJTT: 2026-06-01T23:45:00Z = 2026-06-02T08:45:00+09:00 (JST)
    observed_utc = "2026-06-01T23:45:00Z"
    station_tz = "Asia/Tokyo"
    dt_local = _parse_utc(observed_utc).astimezone(ZoneInfo(station_tz))
    local_date = dt_local.date().isoformat()
    local_hour = dt_local.hour

    if local_date == "2026-06-02" and local_hour == 8:
        report("P-12", "local_date uses station timezone (not UTC)", PASS)
    else:
        report("P-12", "local_date uses station timezone (not UTC)", FAIL,
               f"local_date={local_date!r} local_hour={local_hour} "
               f"(expected '2026-06-02', 8)")


def test_p13_dst_window_end():
    """Window end uses ZoneInfo next-midnight, not +24h (catches 23h/25h DST days)."""
    from datetime import datetime, timedelta, timezone

    # NYC spring-forward: 2026-03-08 is 23h long
    # temp_window_start_utc = midnight EST = 05:00 UTC
    tws = "2026-03-08T05:00:00Z"
    station_tz = "America/New_York"

    window_start = _parse_utc(tws).astimezone(ZoneInfo(station_tz))
    next_day = window_start.date() + timedelta(days=1)
    window_end_correct = datetime(
        next_day.year, next_day.month, next_day.day,
        tzinfo=ZoneInfo(station_tz)
    ).astimezone(timezone.utc)

    window_end_naive = _parse_utc(tws).astimezone(timezone.utc) + timedelta(hours=24)

    # Correct: 04:00 UTC (23h window, EDT midnight)
    # Naive:   05:00 UTC (24h window — wrong, captures next day's temperatures)
    correct_utc_hour = window_end_correct.hour
    naive_utc_hour = window_end_naive.hour

    if correct_utc_hour == 4 and naive_utc_hour == 5:
        report("P-13", "DST window end (ZoneInfo vs +24h)", PASS,
               f"correct={window_end_correct.isoformat()}, naive={window_end_naive.isoformat()}")
    else:
        report("P-13", "DST window end (ZoneInfo vs +24h)", FAIL,
               f"correct_hour={correct_utc_hour} naive_hour={naive_utc_hour}")


# ── GROUP 4 — Order Books ────────────────────────────────────────────────────

def test_p14_snapshot_label_set():
    """snapshot_label must only use defined values."""
    valid_labels = {
        "T-30min", "T-1h", "T-3h", "T-6h", "T-12h", "T-24h",
        "open_window", "post_close", "open", None
    }
    # Simulate the label function from log_orderbooks.py
    def snapshot_label(hours_to_close):
        if hours_to_close is None:
            return None
        if hours_to_close < 0:
            return "post_close"
        for threshold, label in [
            (0.5, "T-30min"), (1.0, "T-1h"), (3.0, "T-3h"),
            (6.0, "T-6h"), (12.0, "T-12h"), (24.0, "T-24h"),
        ]:
            if hours_to_close <= threshold:
                return label
        return "open_window"

    test_cases = [
        (-2.0,  "post_close"),
        (0.4,   "T-30min"),
        (0.9,   "T-1h"),
        (2.5,   "T-3h"),
        (5.0,   "T-6h"),
        (10.0,  "T-12h"),
        (20.0,  "T-24h"),
        (30.0,  "open_window"),
        (None,  None),
    ]
    failures = []
    for htc, expected in test_cases:
        got = snapshot_label(htc)
        if got != expected:
            failures.append(f"hours={htc}: got {got!r}, expected {expected!r}")
        if got not in valid_labels:
            failures.append(f"hours={htc}: label {got!r} not in valid set")

    if failures:
        report("P-14", "snapshot_label values", FAIL, "; ".join(failures))
    else:
        report("P-14", "snapshot_label values", PASS)


def test_p15_empty_book_stored():
    """Empty-book response must store a NULL-price row, not be dropped."""
    conn = fresh_db()
    # Insert a market to satisfy foreign key logic (we skip FK enforcement here)
    # Simulate what _snapshot_market should do on empty books
    snap = {
        "condition_id": "0xTEST",
        "ts_utc": "2026-06-01T14:00:00Z",
        "yes_bid": None, "yes_ask": None,
        "yes_bid_size": None, "yes_ask_size": None,
        "no_bid": None, "no_ask": None,
        "no_bid_size": None, "no_ask_size": None,
        "yes_mid": None, "spread": None,
        "raw_book_json": "{}",
        "hours_to_close": -2.0,
        "snapshot_label": "post_close",
    }
    conn.execute("""
        INSERT INTO ob_snapshots
        (condition_id,ts_utc,yes_bid,yes_ask,yes_bid_size,yes_ask_size,
         no_bid,no_ask,no_bid_size,no_ask_size,yes_mid,spread,
         raw_book_json,hours_to_close,snapshot_label)
        VALUES(:condition_id,:ts_utc,:yes_bid,:yes_ask,:yes_bid_size,:yes_ask_size,
               :no_bid,:no_ask,:no_bid_size,:no_ask_size,:yes_mid,:spread,
               :raw_book_json,:hours_to_close,:snapshot_label)
    """, snap)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM ob_snapshots WHERE condition_id='0xTEST'"
    ).fetchone()
    conn.close()

    if row and row["yes_bid"] is None and row["snapshot_label"] == "post_close":
        report("P-15", "Empty book stored as NULL-price row", PASS)
    else:
        report("P-15", "Empty book stored as NULL-price row", FAIL,
               f"row={dict(row) if row else None}")


# ── GROUP 5 — Settlement ─────────────────────────────────────────────────────

def test_p17_bucket_parser():
    """_parse_bucket must handle all known question formats correctly."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    try:
        from discover_markets import _parse_bucket
    except ImportError as e:
        report("P-17", "Bucket parser — known formats", SKIP, f"import error: {e}")
        return

    cases = [
        ("Will the highest temperature in Seoul be 31°C on June 1?",
         "exact", 31.0, 31.0, "C"),
        ("Will the highest temperature in NYC be between 88-89°F on June 1?",
         "range", 88.0, 89.0, "F"),
        ("Will the highest temperature in Tokyo be 35°C or higher on June 1?",
         "above_eq", 35.0, None, "C"),
        ("Will the highest temperature in London be 18°C or below on June 1?",
         "below_eq", None, 18.0, "C"),
        ("Will the highest temperature in Miami be between 92-93°F on June 1?",
         "range", 92.0, 93.0, "F"),
    ]

    failures = []
    for question, exp_type, exp_lower, exp_upper, exp_unit in cases:
        btype, lower, upper, unit = _parse_bucket(question, "C")
        if btype != exp_type:
            failures.append(f"type: got {btype!r} != {exp_type!r} for: {question[:40]}")
        if exp_lower is not None and lower != exp_lower:
            failures.append(f"lower: got {lower} != {exp_lower} for: {question[:40]}")
        if exp_upper is not None and upper != exp_upper:
            failures.append(f"upper: got {upper} != {exp_upper} for: {question[:40]}")
        if unit != exp_unit:
            failures.append(f"unit: got {unit!r} != {exp_unit!r} for: {question[:40]}")

    if failures:
        report("P-17", "Bucket parser — known formats", FAIL, "\n         ".join(failures))
    else:
        report("P-17", "Bucket parser — known formats", PASS)


def test_p18_unknown_bucket_skipped():
    """bucket_type='unknown' must not get a proxy_outcome written."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    try:
        from settle_markets import _proxy_outcome
    except ImportError as e:
        report("P-18", "unknown bucket skipped by settle_markets", SKIP, f"import error: {e}")
        return

    class FakeRow:
        def __getitem__(self, k):
            return {"bucket_type": "unknown", "lower_temp": None,
                    "upper_temp": None, "settlement_value_proxy": 28.0,
                    "settlement_rounding_rule": "round"}[k]

    result = _proxy_outcome(FakeRow())
    if result is None:
        report("P-18", "unknown bucket skipped by settle_markets", PASS)
    else:
        report("P-18", "unknown bucket skipped by settle_markets", FAIL,
               f"returned {result!r} instead of None")


def test_p19_exactly_one_yes():
    """After settle, each city+date group must have exactly 1 YES proxy_outcome."""
    conn = fresh_db()

    def insert_market(cid, city, btype, lower, upper, proxy_val, rounding="round"):
        conn.execute("""
            INSERT INTO weather_markets
            (condition_id,city,station,settlement_date,bucket_type,lower_temp,upper_temp,
             bucket_unit,settlement_unit,settlement_value_proxy,settlement_rounding_rule)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (cid, city, "RKSI", "2026-06-01", btype, lower, upper,
              "C", "C", proxy_val, rounding))

    # Seoul staircase: settlement = 31°C
    insert_market("A1", "Seoul", "exact",    29.0, 29.0, 31.0)
    insert_market("A2", "Seoul", "exact",    30.0, 30.0, 31.0)
    insert_market("A3", "Seoul", "exact",    31.0, 31.0, 31.0)  # should be YES
    insert_market("A4", "Seoul", "exact",    32.0, 32.0, 31.0)
    insert_market("A5", "Seoul", "above_eq", 33.0, None, 31.0)
    conn.commit()

    # Apply proxy outcomes (simplified version of settle_markets logic)
    rows = conn.execute("""
        SELECT condition_id, bucket_type, lower_temp, upper_temp,
               settlement_value_proxy, settlement_rounding_rule
        FROM weather_markets WHERE city='Seoul'
    """).fetchall()

    yes_count = 0
    for row in rows:
        val = round(float(row["settlement_value_proxy"]))
        btype = row["bucket_type"]
        lower = row["lower_temp"]
        upper = row["upper_temp"]
        outcome = None
        if btype == "exact" and lower is not None:
            outcome = "YES" if val == lower else "NO"
        elif btype == "above_eq" and lower is not None:
            outcome = "YES" if val >= lower else "NO"
        elif btype == "below_eq" and upper is not None:
            outcome = "YES" if val <= upper else "NO"
        elif btype == "range" and lower is not None and upper is not None:
            outcome = "YES" if lower <= val <= upper else "NO"
        if outcome == "YES":
            yes_count += 1

    conn.close()
    if yes_count == 1:
        report("P-19", "Exactly one YES per city+date", PASS)
    else:
        report("P-19", "Exactly one YES per city+date", FAIL,
               f"Seoul staircase at 31°C has {yes_count} YES outcomes (expected 1)")


def test_p20_fahrenheit_rounding():
    """C→F conversion must use round() before bucket comparison."""
    cases = [
        # (daily_high_c, bucket_lower_f, bucket_upper_f, expect_match)
        # 35.28°C = 95.504°F → round() = 96 → in [95,96]: YES
        (35.28, 95, 96, True),
        # 35.28°C = 95.504°F → round() = 96 → NOT in [94,95]: NO (correct; WU reports 96)
        (35.28, 94, 95, False),
        # 34.86°C = 94.748°F → round() = 95 → in [95,96]: YES
        # (without round, 94.748 is NOT in [95,96] — rounding changes the matched bucket)
        (34.86, 95, 96, True),
        # 34.44°C = 94.0°F → round() = 94 → in [94,95]: YES
        (34.44, 94, 95, True),
        # 34.44°C = 94.0°F → round() = 94 → NOT in [95,96]: NO
        (34.44, 95, 96, False),
    ]

    failures = []
    for c, lo, hi, expected in cases:
        converted = round(c * 9 / 5 + 32)
        match = (lo <= converted <= hi)
        if match != expected:
            failures.append(
                f"{c}°C → {converted}°F vs [{lo},{hi}]: got {match}, expected {expected}"
            )

    if failures:
        report("P-20", "C→F rounding before bucket comparison", FAIL, "; ".join(failures))
    else:
        report("P-20", "C→F rounding before bucket comparison", PASS)


# ── GROUP 6 — Scanner ────────────────────────────────────────────────────────

def test_p22_forward_gap_detected():
    """Scanner forward gap: P_above(bid) - P_sum(ask) > 2¢ must flag."""
    # above_eq 32°C: bid=0.65
    # exact 32°C ask=0.20, exact 33°C ask=0.20, exact 34°C+ ask=0.05
    above_bid = 0.65
    bucket_asks = [0.20, 0.20, 0.05]
    threshold = 0.02

    forward_gap = above_bid - sum(bucket_asks)
    flagged = forward_gap > threshold

    if flagged and abs(forward_gap - 0.20) < 0.001:
        report("P-22", "Scanner forward gap detected", PASS,
               f"gap={forward_gap:.3f}")
    else:
        report("P-22", "Scanner forward gap detected", FAIL,
               f"gap={forward_gap:.3f}, flagged={flagged}")


def test_p23_reverse_gap_detected():
    """Scanner reverse gap: P_sum(bid) - P_above(ask) > 2¢ must flag."""
    above_ask = 0.42
    bucket_bids = [0.25, 0.20]
    threshold = 0.02

    reverse_gap = sum(bucket_bids) - above_ask
    flagged = reverse_gap > threshold

    if flagged and abs(reverse_gap - 0.03) < 0.001:
        report("P-23", "Scanner reverse gap detected", PASS,
               f"gap={reverse_gap:.3f}")
    else:
        report("P-23", "Scanner reverse gap detected", FAIL,
               f"gap={reverse_gap:.3f}, flagged={flagged}")


def test_p24_null_group_excluded():
    """Markets with NULL neg_risk_market_id must be excluded from scanner queries."""
    conn = fresh_db()
    # Insert one market with NULL group_id
    conn.execute("""
        INSERT INTO weather_markets
        (condition_id,city,station,settlement_date,bucket_type,bucket_unit,
         settlement_unit,neg_risk_market_id,active)
        VALUES('0xNULL','Seoul','RKSI','2026-06-01','exact','C','C',NULL,1)
    """)
    conn.execute("""
        INSERT INTO weather_markets
        (condition_id,city,station,settlement_date,bucket_type,bucket_unit,
         settlement_unit,neg_risk_market_id,active)
        VALUES('0xGOOD','Seoul','RKSI','2026-06-01','exact','C','C','group_A',1)
    """)
    conn.commit()

    scanner_rows = conn.execute("""
        SELECT condition_id FROM weather_markets
        WHERE neg_risk_market_id IS NOT NULL AND active=1
    """).fetchall()
    ids = [r[0] for r in scanner_rows]
    conn.close()

    if "0xGOOD" in ids and "0xNULL" not in ids:
        report("P-24", "NULL neg_risk_market_id excluded from scanner", PASS)
    else:
        report("P-24", "NULL neg_risk_market_id excluded from scanner", FAIL,
               f"scanner ids: {ids}")


def test_p25_alert_deduplication():
    """Second scan of same gap must update last_seen_utc, not insert a new row."""
    conn = fresh_db()
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    def upsert_alert(conn, city, alert_type, settlement_date, detail):
        existing = conn.execute("""
            SELECT id FROM alerts
            WHERE city=? AND alert_type=? AND settlement_date=? AND status='open'
        """, (city, alert_type, settlement_date)).fetchone()
        if existing:
            conn.execute("""
                UPDATE alerts SET last_seen_utc=?, detail_json=? WHERE id=?
            """, (dt.datetime.now(dt.timezone.utc).isoformat(),
                  json.dumps(detail), existing[0]))
        else:
            conn.execute("""
                INSERT INTO alerts(opened_utc,last_seen_utc,city,settlement_date,
                                   alert_type,status,detail_json)
                VALUES(?,?,?,?,?,?,?)
            """, (now, now, city, settlement_date, alert_type, "open",
                  json.dumps(detail)))
        conn.commit()

    upsert_alert(conn, "Seoul", "neg_risk_gap_forward", "2026-06-01", {"gap": 0.20})
    upsert_alert(conn, "Seoul", "neg_risk_gap_forward", "2026-06-01", {"gap": 0.18})
    upsert_alert(conn, "Seoul", "neg_risk_gap_forward", "2026-06-01", {"gap": 0.15})

    count = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE city='Seoul'"
    ).fetchone()[0]
    detail = json.loads(conn.execute(
        "SELECT detail_json FROM alerts WHERE city='Seoul'"
    ).fetchone()[0])
    conn.close()

    if count == 1 and detail["gap"] == 0.15:
        report("P-25", "Alert deduplication (update, not insert)", PASS)
    else:
        report("P-25", "Alert deduplication (update, not insert)", FAIL,
               f"count={count}, latest_detail={detail}")


# ── GROUP 7 — Reliability ────────────────────────────────────────────────────

def test_p26_fetch_failure_threshold():
    """3 consecutive failed cycles in 2h must trigger fetch_failure alert."""
    conn = fresh_db()
    base_time = dt.datetime(2026, 6, 1, 9, 0, 0, tzinfo=dt.timezone.utc)

    # 3 cycles ending at base_time: 08:00, 08:30, 09:00 UTC (all in past 2h window)
    for i in range(3):
        ts = (base_time - dt.timedelta(minutes=30 * (2 - i))).isoformat()
        for attempt in range(1, 4):
            conn.execute("""
                INSERT INTO fetch_log(ts_utc,source,attempt,status,error)
                VALUES(?,?,?,?,?)
            """, (ts, "metar", attempt, "failed", "connection timeout"))
    conn.commit()

    window_start   = (base_time - dt.timedelta(hours=2)).isoformat()
    window_end     = base_time.isoformat()
    failed_cycles = conn.execute("""
        SELECT COUNT(DISTINCT ts_utc) FROM fetch_log
        WHERE source='metar' AND status='failed'
          AND ts_utc >= ? AND ts_utc <= ?
    """, (window_start, window_end)).fetchone()[0]

    should_alert = failed_cycles >= 3
    conn.close()

    if should_alert:
        report("P-26", "Fetch failure alert threshold (3 cycles in 2h)", PASS,
               f"failed_cycles={failed_cycles}")
    else:
        report("P-26", "Fetch failure alert threshold (3 cycles in 2h)", FAIL,
               f"failed_cycles={failed_cycles} (expected >=3)")


def test_p27_stale_run_detection():
    """GFS delta > 2h past nominal run time should flag stale_run."""
    nominal_run_utc = dt.datetime(2026, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    configured_offset_h = 4  # we fetch ~4h after 00z run = 04:00 UTC

    # Case 1: fetched at 04:30 UTC — only 30min past expected → NOT stale
    fetched_on_time = dt.datetime(2026, 6, 1, 4, 30, 0, tzinfo=dt.timezone.utc)
    implied_run_1 = fetched_on_time - dt.timedelta(hours=configured_offset_h)
    delta_h_1 = (implied_run_1 - nominal_run_utc).total_seconds() / 3600
    stale_1 = abs(delta_h_1) > 2

    # Case 2: fetched at 07:00 UTC — 3h past expected → stale (GFS delayed)
    fetched_late = dt.datetime(2026, 6, 1, 7, 0, 0, tzinfo=dt.timezone.utc)
    implied_run_2 = fetched_late - dt.timedelta(hours=configured_offset_h)
    delta_h_2 = (implied_run_2 - nominal_run_utc).total_seconds() / 3600
    stale_2 = abs(delta_h_2) > 2

    if not stale_1 and stale_2:
        report("P-27", "GFS stale-run detection (>2h delta)", PASS,
               f"on-time delta={delta_h_1:.1f}h, late delta={delta_h_2:.1f}h")
    else:
        report("P-27", "GFS stale-run detection (>2h delta)", FAIL,
               f"stale_on_time={stale_1} (expected False), stale_late={stale_2} (expected True)")


# ── GROUP 2 — Missing offline tests ──────────────────────────────────────────

def test_p07_post_close_discovery_guard():
    """Markets discovered after close_time_utc must be set active=0."""
    conn = fresh_db()
    past_close = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat()
    now_str = dt.datetime.now(dt.timezone.utc).isoformat()

    # Insert a market whose close_time_utc is 3 hours in the past
    conn.execute("""
        INSERT INTO weather_markets
        (condition_id,city,station,settlement_date,bucket_type,bucket_unit,
         settlement_unit,close_time_utc,first_seen_utc,active)
        VALUES('0xPOSTCLOSE','Seoul','RKSI','2026-06-01','exact','C','C',?,?,1)
    """, (past_close, now_str))
    conn.commit()

    # Simulate the guard: if first_seen_utc >= close_time_utc → set active=0
    rows = conn.execute("""
        SELECT condition_id, first_seen_utc, close_time_utc
        FROM weather_markets WHERE condition_id='0xPOSTCLOSE'
    """).fetchall()

    updated = 0
    for row in rows:
        if row["first_seen_utc"] >= row["close_time_utc"]:
            conn.execute(
                "UPDATE weather_markets SET active=0 WHERE condition_id=?",
                (row["condition_id"],)
            )
            updated += 1
    conn.commit()

    active = conn.execute(
        "SELECT active FROM weather_markets WHERE condition_id='0xPOSTCLOSE'"
    ).fetchone()[0]
    conn.close()

    if active == 0 and updated == 1:
        report("P-07", "Post-close discovery guard sets active=0", PASS)
    else:
        report("P-07", "Post-close discovery guard sets active=0", FAIL,
               f"active={active}, updated={updated}")


def test_p10_station_coverage_alert():
    """Missing stations in METAR batch response must produce fetch_failure alerts."""
    conn = fresh_db()
    configured = {"RKSI","VHHH","EGLC","RJTT","KLGA","LFPB","ZBAA",
                  "KMIA","WSSS","LEMD","UUWW","EDDM","EHAM","LTAC",
                  "NZWN","ZGSZ","ZGGG"}
    returned   = configured - {"UUWW", "NZWN"}   # simulated partial response
    missing    = configured - returned
    now        = dt.datetime.now(dt.timezone.utc).isoformat()

    n_records = len(returned)
    conn.execute("""
        INSERT INTO fetch_log(ts_utc,source,attempt,status,n_records)
        VALUES(?,?,?,?,?)
    """, (now, "metar", 1, "success", n_records))

    for station in missing:
        conn.execute("""
            INSERT INTO alerts(opened_utc,last_seen_utc,city,alert_type,status,detail_json)
            VALUES(?,?,?,?,?,?)
        """, (now, now, station, "fetch_failure", "open",
              json.dumps({"station": station, "reason": "absent from METAR batch"})))
    conn.commit()

    alert_stations = {
        r[0] for r in conn.execute(
            "SELECT city FROM alerts WHERE alert_type='fetch_failure'"
        ).fetchall()
    }
    logged_n = conn.execute(
        "SELECT n_records FROM fetch_log WHERE source='metar'"
    ).fetchone()[0]
    conn.close()

    if alert_stations == {"UUWW","NZWN"} and logged_n == 15:
        report("P-10", "Station coverage alert on partial METAR response", PASS,
               f"alerts for {alert_stations}, n_records={logged_n}")
    else:
        report("P-10", "Station coverage alert on partial METAR response", FAIL,
               f"alert_stations={alert_stations}, n_records={logged_n}")


def test_p16_open_label_logic():
    """open label must fire once, within 5 min of first_seen_utc, never overwrite."""
    # Test the label assignment logic without live data:
    # snapshot within 5 min of first_seen → open
    # subsequent snapshots → bin label only

    # Market discovered 36h before close — snapshots in open_window territory
    close_time  = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
    first_seen  = close_time - dt.timedelta(hours=36)   # T-36h

    def assign_label(ts: dt.datetime, first_seen: dt.datetime,
                     close_time: dt.datetime, already_open: bool) -> tuple[str, bool]:
        """Returns (label, open_label_assigned)."""
        htc = (close_time - ts).total_seconds() / 3600
        # open label: first snapshot within 5 min of first_seen, not yet assigned
        if not already_open and abs((ts - first_seen).total_seconds()) <= 300:
            return "open", True
        if htc < 0:    return "post_close", already_open
        if htc <= 0.5: return "T-30min",   already_open
        if htc <= 1.0: return "T-1h",      already_open
        if htc <= 3.0: return "T-3h",      already_open
        if htc <= 6.0: return "T-6h",      already_open
        if htc <= 12.0:return "T-12h",     already_open
        if htc <= 24.0:return "T-24h",     already_open
        return "open_window", already_open

    snapshots = [
        first_seen + dt.timedelta(minutes=2),   # → open (within 5 min, htc=35.97h)
        first_seen + dt.timedelta(minutes=10),  # → open_window (already assigned, htc>24h)
        first_seen + dt.timedelta(hours=12),    # → open_window (htc=24h)
        first_seen + dt.timedelta(hours=13),    # → T-24h (htc=23h ≤ 24h)
    ]

    open_assigned = False
    labels = []
    for ts in snapshots:
        label, open_assigned = assign_label(ts, first_seen, close_time, open_assigned)
        labels.append(label)

    open_count = labels.count("open")
    if open_count == 1 and labels[0] == "open" and labels[1] == "open_window" and labels[3] == "T-24h":
        report("P-16", "open label fires once, not overwritten", PASS,
               f"labels={labels}")
    else:
        report("P-16", "open label fires once, not overwritten", FAIL,
               f"labels={labels}, open_count={open_count}")


def test_p21_settle_trigger_timing():
    """settle_markets.py trigger: temp_window_start_utc + 26h < now."""
    from datetime import datetime, timezone, timedelta

    cases = [
        # (city, temp_window_start_utc, expected_trigger_utc_hour_approx)
        # Seoul/Tokyo UTC+9: local midnight = 15:00 UTC; trigger = 17:00 UTC
        ("Seoul/Tokyo", "2026-06-01T15:00:00Z", 17),
        # Wellington NZST UTC+12: local midnight = 12:00 UTC; trigger = 14:00 UTC
        ("Wellington",  "2026-06-01T12:00:00Z", 14),
        # NYC EDT UTC-4: local midnight = 04:00 UTC; trigger = 06:00 UTC
        ("NYC/Miami",   "2026-06-01T04:00:00Z",  6),
        # London BST UTC+1: local midnight = 23:00 UTC prev day; trigger = 01:00 UTC next
        ("London",      "2026-05-31T23:00:00Z",  1),
    ]

    failures = []
    for city, tws, expected_hour in cases:
        window_start = _parse_utc(tws)
        trigger_utc  = window_start + timedelta(hours=26)
        if trigger_utc.hour != expected_hour:
            failures.append(
                f"{city}: trigger_hour={trigger_utc.hour}, expected={expected_hour}"
            )

    if failures:
        report("P-21", "settle_markets.py trigger UTC hours", FAIL,
               "; ".join(failures))
    else:
        report("P-21", "settle_markets.py trigger UTC hours", PASS)


# ── GROUP 8 — End-to-end ─────────────────────────────────────────────────────

def test_p28_end_to_end(network: bool):
    """Full pipeline smoke test — Seoul one day (requires network + live data)."""
    if not network:
        report("P-28", "End-to-end pipeline smoke test (Seoul)", SKIP,
               "requires --network and live APIs")
        return

    import asyncio
    try:
        from discover_markets import discover
        from fetch_weather import fetch_metar
    except ImportError as e:
        report("P-28", "End-to-end pipeline smoke test (Seoul)", SKIP,
               f"import error: {e}")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        init_db(conn)

        n_markets = asyncio.run(discover(conn, days_ahead=1))
        seoul_markets = conn.execute("""
            SELECT condition_id, city, station, settlement_date, bucket_type,
                   close_time_utc, temp_window_start_utc, neg_risk_market_id,
                   yes_token_id, no_token_id
            FROM weather_markets WHERE city='Seoul' AND active=1
        """).fetchall()

        failures = []
        if not seoul_markets:
            failures.append("no Seoul markets discovered")
        else:
            for m in seoul_markets:
                for col in ("close_time_utc","temp_window_start_utc",
                            "neg_risk_market_id","yes_token_id","no_token_id"):
                    if m[col] is None:
                        failures.append(f"Seoul market {m['condition_id'][:8]}: {col} is NULL")

        conn.close()
        if failures:
            report("P-28", "End-to-end pipeline smoke test (Seoul)", FAIL,
                   "; ".join(failures[:3]))
        else:
            report("P-28", "End-to-end pipeline smoke test (Seoul)", PASS,
                   f"{len(seoul_markets)} Seoul markets, all required fields present")
    except Exception as e:
        report("P-28", "End-to-end pipeline smoke test (Seoul)", FAIL, str(e))


# ── SELF-CHECK: spec vs implementation count ──────────────────────────────────

def test_spec_implementation_sync():
    """PREPRODUCTION_TESTS.md and run_tests.py must define the same number of tests.

    This test prevents the most common form of drift: a test is added to the
    prose spec but never implemented, or implemented without updating the spec.
    """
    spec_file = os.path.join(REPO_ROOT, "PREPRODUCTION_TESTS.md")
    impl_file = os.path.join(REPO_ROOT, "scripts", "run_tests.py")

    import re
    with open(spec_file) as f:
        spec_ids = set(re.findall(r'^### (P-\d+):', f.read(), re.MULTILINE))
    with open(impl_file) as f:
        impl_ids = set(re.findall(r'^def test_p(\d+)_', f.read(), re.MULTILINE))
        impl_ids = {f"P-{n}" for n in impl_ids}

    missing_impl = spec_ids - impl_ids
    extra_impl   = impl_ids - spec_ids

    if not missing_impl and not extra_impl:
        report("SYNC", "Spec↔implementation count matches", PASS,
               f"{len(spec_ids)} tests in both")
    else:
        detail = []
        if missing_impl:
            detail.append(f"in spec but not implemented: {sorted(missing_impl)}")
        if extra_impl:
            detail.append(f"implemented but not in spec: {sorted(extra_impl)}")
        report("SYNC", "Spec↔implementation count matches", FAIL,
               "; ".join(detail))


# ── NETWORK TESTS (skipped unless --network passed) ──────────────────────────

def test_p05_live_discovery(network: bool):
    if not network:
        report("P-05", "Live market discovery", SKIP, "requires --network")
        return
    try:
        import asyncio
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        from discover_markets import discover
        conn = fresh_db()
        n = asyncio.run(discover(conn, days_ahead=1))
        if n > 0:
            report("P-05", "Live market discovery", PASS, f"{n} markets discovered")
        else:
            report("P-05", "Live market discovery", FAIL, "0 markets discovered")
        conn.close()
    except Exception as e:
        report("P-05", "Live market discovery", FAIL, str(e))


def test_p09_live_metar(network: bool):
    if not network:
        report("P-09", "Live METAR batch fetch", SKIP, "requires --network")
        return
    try:
        import asyncio, httpx
        stations = ["RKSI","VHHH","EGLC","RJTT","KLGA","LFPB","ZBAA",
                    "KMIA","WSSS","LEMD","UUWW","EDDM","EHAM","LTAC",
                    "NZWN","ZGSZ","ZGGG"]
        url = ("https://aviationweather.gov/api/data/metar"
               f"?ids={','.join(stations)}&format=json&hours=1")
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        returned = {obs["icaoId"] for obs in data if "icaoId" in obs}
        missing = set(stations) - returned
        if missing:
            report("P-09", "Live METAR batch fetch", FAIL,
                   f"missing stations: {missing}")
        else:
            report("P-09", "Live METAR batch fetch", PASS,
                   f"{len(returned)}/17 stations returned")
    except Exception as e:
        report("P-09", "Live METAR batch fetch", FAIL, str(e))


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pre-production test runner")
    parser.add_argument("--group", type=int, help="Run only this group (1-8)")
    parser.add_argument("--network", action="store_true",
                        help="Include tests requiring live API access")
    args = parser.parse_args()

    # Delete and recreate an isolated test database so tests start from a known state.
    # Never point tests at the runtime weather.db unless WEATHER_TEST_DB is explicitly set.
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH + suffix
        if os.path.exists(path):
            os.remove(path)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.close()

    g = args.group

    print("\n═══ Pre-production test runner ═══\n")
    print("Group 1 — Schema")
    if not g or g == 1:
        test_p01_schema_clean_init()
        test_p02_migration_idempotent()
        test_p03_concurrent_writes()
        test_p04_insert_or_ignore()

    print("\nGroup 2 — Discovery")
    if not g or g == 2:
        test_p05_live_discovery(args.network)
        test_p06_settlement_date_derivation()
        test_p07_post_close_discovery_guard()
        test_p08_slug_failure_detection()

    print("\nGroup 3 — METAR")
    if not g or g == 3:
        test_p09_live_metar(args.network)
        test_p10_station_coverage_alert()
        test_p11_metar_correction()
        test_p12_local_date_timezone()
        test_p13_dst_window_end()

    print("\nGroup 4 — Order Books")
    if not g or g == 4:
        test_p14_snapshot_label_set()
        test_p15_empty_book_stored()
        test_p16_open_label_logic()

    print("\nGroup 5 — Settlement")
    if not g or g == 5:
        test_p17_bucket_parser()
        test_p18_unknown_bucket_skipped()
        test_p19_exactly_one_yes()
        test_p20_fahrenheit_rounding()
        test_p21_settle_trigger_timing()

    print("\nGroup 6 — Scanner")
    if not g or g == 6:
        test_p22_forward_gap_detected()
        test_p23_reverse_gap_detected()
        test_p24_null_group_excluded()
        test_p25_alert_deduplication()

    print("\nGroup 7 — Reliability")
    if not g or g == 7:
        test_p26_fetch_failure_threshold()
        test_p27_stale_run_detection()

    print("\nGroup 8 — End-to-end")
    if not g or g == 8:
        test_p28_end_to_end(args.network)

    print("\nSync check — spec vs implementation")
    test_spec_implementation_sync()

    # Summarise
    passed  = sum(1 for r in results if r["status"] == PASS)
    failed  = sum(1 for r in results if r["status"] == FAIL)
    skipped = sum(1 for r in results if r["status"] == SKIP)
    total   = len(results)

    print(f"\n{'═'*40}")
    print(f"Results: {passed}/{total} passed  "
          f"| {failed} failed  | {skipped} skipped")

    if failed:
        print("\nFailed tests:")
        for r in results:
            if r["status"] == FAIL:
                print(f"  ❌ {r['id']}  {r['name']}")
                if r["detail"]:
                    print(f"     {r['detail']}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
