"""
Authoritative SQLite schema for the Polymarket weather project.

Every operational script should call init_db(conn) before reading or writing.
Do not define table layouts independently in job scripts.
"""
from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS wx_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    city TEXT NOT NULL,
    observed_utc TEXT,
    fetched_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    local_hour INTEGER,
    temp_c REAL,
    daily_high_c REAL,
    is_in_peak_window INTEGER DEFAULT 0,
    source TEXT DEFAULT 'metar',
    raw_payload_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_wx_obs_unique
    ON wx_observations(station, source, observed_utc);
CREATE INDEX IF NOT EXISTS ix_wx_station_date
    ON wx_observations(station, local_date);
CREATE INDEX IF NOT EXISTS ix_wx_fetched
    ON wx_observations(fetched_utc);

CREATE TABLE IF NOT EXISTS taf_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    city TEXT NOT NULL,
    issued_utc TEXT NOT NULL,
    valid_from_utc TEXT NOT NULL,
    valid_to_utc TEXT NOT NULL,
    fetched_utc TEXT NOT NULL,
    tx_c REAL,
    tx_time_utc TEXT,
    tn_c REAL,
    tn_time_utc TEXT,
    raw_taf TEXT,
    raw_payload_json TEXT,
    UNIQUE(station, issued_utc)
);

CREATE TABLE IF NOT EXISTS model_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    city TEXT NOT NULL,
    model TEXT NOT NULL,
    model_run_utc TEXT NOT NULL,
    fetched_utc TEXT NOT NULL,
    forecast_date TEXT NOT NULL,
    horizon_hours INTEGER,
    high_c REAL,
    low_c REAL,
    lat REAL,
    lon REAL,
    model_run_is_estimated INTEGER DEFAULT 1,
    raw_payload_json TEXT,
    UNIQUE(station, model, model_run_utc, forecast_date)
);
CREATE INDEX IF NOT EXISTS ix_model_station_date
    ON model_forecasts(station, forecast_date, model);

CREATE TABLE IF NOT EXISTS weather_markets (
    condition_id TEXT PRIMARY KEY,
    event_slug TEXT,
    market_slug TEXT,
    city TEXT NOT NULL,
    station TEXT NOT NULL,
    settlement_date TEXT NOT NULL,
    bucket_type TEXT NOT NULL,
    lower_temp REAL,
    upper_temp REAL,
    bucket_unit TEXT NOT NULL,
    settlement_unit TEXT NOT NULL,
    yes_token_id TEXT,
    no_token_id TEXT,
    question TEXT,
    rules_text TEXT,
    rules_source TEXT,
    resolution_source_type TEXT,
    resolution_source_url TEXT,
    raw_market_json TEXT,
    game_start_time_utc TEXT,
    close_time_utc TEXT,
    accepting_order_ts_utc TEXT,
    neg_risk_market_id TEXT,
    neg_risk_request_id TEXT,
    first_seen_utc TEXT,
    settlement_value_proxy REAL,
    settlement_value_final REAL,
    settlement_source TEXT,
    settlement_rounding_rule TEXT,
    resolution_status TEXT,
    settled_at_utc TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_weather_city_date
    ON weather_markets(city, settlement_date);
CREATE INDEX IF NOT EXISTS ix_weather_neg_risk
    ON weather_markets(neg_risk_market_id);
CREATE INDEX IF NOT EXISTS ix_weather_close
    ON weather_markets(close_time_utc);

CREATE TABLE IF NOT EXISTS ob_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    yes_bid REAL,
    yes_ask REAL,
    yes_bid_size REAL,
    yes_ask_size REAL,
    no_bid REAL,
    no_ask REAL,
    no_bid_size REAL,
    no_ask_size REAL,
    yes_mid REAL,
    spread REAL,
    raw_book_json TEXT,
    hours_to_close REAL,
    snapshot_label TEXT
);
CREATE INDEX IF NOT EXISTS ix_ob_cid_ts
    ON ob_snapshots(condition_id, ts_utc);

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    station TEXT,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    n_records INTEGER DEFAULT 0,
    error TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fetchlog_ts
    ON fetch_log(ts_utc);
CREATE INDEX IF NOT EXISTS ix_fetchlog_source
    ON fetch_log(source, ts_utc);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    city TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS settlement_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT,
    city TEXT NOT NULL,
    station TEXT,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT,
    local_date TEXT NOT NULL,
    value REAL,
    unit TEXT,
    precision TEXT,
    fetched_utc TEXT NOT NULL,
    raw_payload_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_settlement_city_date
    ON settlement_observations(city, local_date, source_type);

CREATE TABLE IF NOT EXISTS market_resolutions (
    condition_id TEXT PRIMARY KEY,
    resolved_outcome TEXT NOT NULL,
    resolved_value REAL,
    resolved_unit TEXT,
    resolution_status TEXT,
    resolved_at_utc TEXT,
    raw_payload_json TEXT
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    _migrate_existing_tables(conn)
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    if not _table_exists(conn, table):
        return
    existing = _columns(conn, table)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate_existing_tables(conn: sqlite3.Connection) -> None:
    """Add columns needed by the current schema to older local databases."""
    _add_missing_columns(conn, "wx_observations", {
        "observed_utc": "TEXT",
        "fetched_utc": "TEXT",
        "local_date": "TEXT",
        "local_hour": "INTEGER",
        "is_in_peak_window": "INTEGER DEFAULT 0",
        "raw_payload_json": "TEXT",
    })
    _add_missing_columns(conn, "taf_forecasts", {
        "raw_payload_json": "TEXT",
    })
    _add_missing_columns(conn, "model_forecasts", {
        "model_run_is_estimated": "INTEGER DEFAULT 1",
        "raw_payload_json": "TEXT",
    })
    _add_missing_columns(conn, "weather_markets", {
        "event_slug": "TEXT",
        "market_slug": "TEXT",
        "lower_temp": "REAL",
        "upper_temp": "REAL",
        "bucket_unit": "TEXT",
        "settlement_unit": "TEXT",
        "rules_text": "TEXT",
        "rules_source": "TEXT",
        "resolution_source_type": "TEXT",
        "resolution_source_url": "TEXT",
        "raw_market_json": "TEXT",
        "game_start_time_utc": "TEXT",
        "close_time_utc": "TEXT",
        "accepting_order_ts_utc": "TEXT",
        "neg_risk_market_id": "TEXT",
        "neg_risk_request_id": "TEXT",
        "first_seen_utc": "TEXT",
        "settlement_value_proxy": "REAL",
        "settlement_value_final": "REAL",
        "settlement_source": "TEXT",
        "settlement_rounding_rule": "TEXT",
        "resolution_status": "TEXT",
        "settled_at_utc": "TEXT",
    })
    _add_missing_columns(conn, "ob_snapshots", {
        "yes_bid_size": "REAL",
        "yes_ask_size": "REAL",
        "no_bid_size": "REAL",
        "no_ask_size": "REAL",
        "spread": "REAL",
        "raw_book_json": "TEXT",
        "hours_to_close": "REAL",
        "snapshot_label": "TEXT",
    })
    conn.commit()


if __name__ == "__main__":
    import os

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(repo_root, "weather.db")
    with sqlite3.connect(db_path) as conn:
        init_db(conn)
    print(f"Initialized {db_path}")
