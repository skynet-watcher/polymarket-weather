"""
fetch_weather.py
================
Collects two categories of data for each of the 16 Polymarket settlement airports:

  GROUND TRUTH
    METAR  — live airport instrument reading every 30 min (aviationweather.gov)
             same data source Weather Underground uses to settle markets

  FORECASTS (48h horizon, fetched at each model's actual update cadence)
    TAF          — official aviation forecast for exact station (where issued)
                   30h window, updated 4x/day at 00/06/12/18 UTC
    GFS          — NOAA, updates 4x/day,  fetch delay ~4h after run
    ECMWF IFS    — European Centre, updates 2x/day, fetch delay ~8h after run
    ICON         — DWD Germany, updates 4x/day, fetch delay ~3h after run
    Météo-France — updates 4x/day, fetch delay ~3.5h after run
    GEM          — Environment Canada, updates 4x/day, fetch delay ~3.5h after run

RETRY POLICY
    Every fetch is attempted up to 3 times.
    On failure, wait RETRY_WAIT_S (180s) before retrying.
    Every attempt — success or failure — is logged to fetch_log with exact timestamp.

Usage:
    cd /Users/eric/polymarket-weather
    .venv/bin/python scripts/fetch_weather.py --metar          # one METAR pass
    .venv/bin/python scripts/fetch_weather.py --forecasts      # one forecast pass
    .venv/bin/python scripts/fetch_weather.py --loop           # full scheduled loop
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import sqlite3
from typing import Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wx_fetch")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH   = os.path.join(REPO_ROOT, "weather.db")

MAX_ATTEMPTS   = 3
RETRY_WAIT_S   = 180   # 3 minutes between retries
AVWX_BASE      = "https://aviationweather.gov/api/data"
OPEN_METEO     = "https://api.open-meteo.com/v1/forecast"

# ── Settlement stations ────────────────────────────────────────────────────────

STATIONS: dict[str, dict] = {
    "RKSI": {"city": "Seoul",      "lat": 37.469,  "lon": 126.451, "anomaly": False},
    "ZBAA": {"city": "Beijing",    "lat": 40.080,  "lon": 116.603, "anomaly": False},
    "EGLC": {"city": "London",     "lat": 51.505,  "lon":   0.055, "anomaly": False},
    "RJTT": {"city": "Tokyo",      "lat": 35.549,  "lon": 139.780, "anomaly": False},
    "KLGA": {"city": "NYC",        "lat": 40.777,  "lon": -73.873, "anomaly": False},
    "LFPB": {"city": "Paris",      "lat": 48.969,  "lon":   2.441, "anomaly": False},
    "KMIA": {"city": "Miami",      "lat": 25.796,  "lon": -80.287, "anomaly": False},
    "WSSS": {"city": "Singapore",  "lat":  1.364,  "lon": 103.992, "anomaly": False},
    "LEMD": {"city": "Madrid",     "lat": 40.472,  "lon":  -3.563, "anomaly": False},
    "EFHK": {"city": "Moscow",     "lat": 60.317,  "lon":  24.963, "anomaly": True},  # HK anomaly
    "EDDM": {"city": "Munich",     "lat": 48.354,  "lon":  11.786, "anomaly": False},
    "EHAM": {"city": "Amsterdam",  "lat": 52.309,  "lon":   4.764, "anomaly": False},
    "LTAC": {"city": "Ankara",     "lat": 40.128,  "lon":  33.001, "anomaly": False},
    "NZWN": {"city": "Wellington", "lat": -41.327, "lon": 174.805, "anomaly": False},
    "ZGSZ": {"city": "Shenzhen",   "lat": 22.639,  "lon": 113.811, "anomaly": False},
    "ZGGG": {"city": "Guangzhou",  "lat": 23.392,  "lon": 113.299, "anomaly": False},
}

# Hong Kong resolves on ZBAA — log separately
HK_STATION = "ZBAA"

# Model update schedule: fetch_offset_h = hours after model run time to fetch
MODELS: dict[str, dict] = {
    "gfs_seamless":          {"runs_utc": [0, 6, 12, 18], "fetch_offset_h": 4},
    "ecmwf_ifs025":          {"runs_utc": [0, 12],         "fetch_offset_h": 8},
    "icon_seamless":         {"runs_utc": [0, 6, 12, 18], "fetch_offset_h": 3},
    "meteofrance_seamless":  {"runs_utc": [0, 6, 12, 18], "fetch_offset_h": 3.5},
    "gem_seamless":          {"runs_utc": [0, 6, 12, 18], "fetch_offset_h": 3.5},
}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS wx_observations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        station         TEXT NOT NULL,
        city            TEXT NOT NULL,
        ts_utc          TEXT NOT NULL,
        temp_c          REAL,
        daily_high_c    REAL,
        source          TEXT DEFAULT 'metar'
    );
    CREATE INDEX IF NOT EXISTS ix_wx_station_ts ON wx_observations(station, ts_utc);

    CREATE TABLE IF NOT EXISTS taf_forecasts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        station         TEXT NOT NULL,
        city            TEXT NOT NULL,
        issued_utc      TEXT NOT NULL,
        valid_from_utc  TEXT NOT NULL,
        valid_to_utc    TEXT NOT NULL,
        tx_c            REAL,
        tx_time_utc     TEXT,
        tn_c            REAL,
        tn_time_utc     TEXT,
        raw_taf         TEXT,
        fetched_utc     TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ix_taf_station_issued
        ON taf_forecasts(station, issued_utc);

    CREATE TABLE IF NOT EXISTS model_forecasts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        station         TEXT NOT NULL,
        city            TEXT NOT NULL,
        model           TEXT NOT NULL,
        model_run_utc   TEXT,
        fetched_utc     TEXT NOT NULL,
        forecast_date   TEXT NOT NULL,
        horizon_hours   INTEGER,
        high_c          REAL,
        low_c           REAL,
        lat             REAL,
        lon             REAL
    );
    CREATE INDEX IF NOT EXISTS ix_mf_station_model
        ON model_forecasts(station, model, fetched_utc);

    CREATE TABLE IF NOT EXISTS fetch_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc      TEXT NOT NULL,       -- exact UTC timestamp of this attempt
        source      TEXT NOT NULL,       -- metar / taf / gfs_seamless / ecmwf_ifs025 / etc.
        station     TEXT,                -- ICAO or 'batch' for multi-station calls
        attempt     INTEGER NOT NULL,    -- 1, 2, or 3
        status      TEXT NOT NULL,       -- success / retry / failed
        n_records   INTEGER DEFAULT 0,   -- rows saved on success
        error       TEXT,               -- error message on failure
        duration_ms INTEGER             -- how long the fetch took
    );
    CREATE INDEX IF NOT EXISTS ix_fetchlog_ts ON fetch_log(ts_utc);
    CREATE INDEX IF NOT EXISTS ix_fetchlog_source ON fetch_log(source, ts_utc);
    """)
    conn.commit()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _log_fetch(conn: sqlite3.Connection, ts: str, source: str,
               station: str, attempt: int, status: str,
               n_records: int = 0, error: str | None = None,
               duration_ms: int | None = None) -> None:
    conn.execute("""
        INSERT INTO fetch_log (ts_utc, source, station, attempt, status, n_records, error, duration_ms)
        VALUES (?,?,?,?,?,?,?,?)
    """, (ts, source, station, attempt, status, n_records, error, duration_ms))
    conn.commit()
    icon = "✓" if status == "success" else ("↻" if status == "retry" else "✗")
    log.info("%s  %-22s %-8s  attempt=%d  saved=%d%s",
             icon, source, station or "batch", attempt, n_records,
             f"  err={error[:60]}" if error else "")


def _running_daily_high(conn: sqlite3.Connection, station: str) -> float | None:
    today = dt.date.today().isoformat()
    row = conn.execute("""
        SELECT MAX(temp_c) FROM wx_observations
        WHERE station=? AND source='metar' AND DATE(ts_utc)=?
    """, (station, today)).fetchone()
    return row[0] if row else None


# ── Retry wrapper ──────────────────────────────────────────────────────────────

async def with_retry(
    fn,
    conn: sqlite3.Connection,
    source: str,
    station: str = "batch",
    max_attempts: int = MAX_ATTEMPTS,
    retry_wait_s: int = RETRY_WAIT_S,
):
    """
    Call async fn(). On exception, log the failure, wait retry_wait_s, and retry.
    Logs every attempt with exact UTC timestamp.
    Returns fn() result on success, None after all attempts exhausted.
    """
    for attempt in range(1, max_attempts + 1):
        ts = _now()
        t0 = dt.datetime.now()
        try:
            result = await fn()
            duration_ms = int((dt.datetime.now() - t0).total_seconds() * 1000)
            n = result if isinstance(result, int) else (len(result) if result else 0)
            _log_fetch(conn, ts, source, station, attempt, "success", n, None, duration_ms)
            return result
        except Exception as e:
            duration_ms = int((dt.datetime.now() - t0).total_seconds() * 1000)
            is_last = (attempt == max_attempts)
            status = "failed" if is_last else "retry"
            _log_fetch(conn, ts, source, station, attempt, status, 0, str(e), duration_ms)
            if not is_last:
                log.warning("  ↻ retry in %ds  (%s attempt %d/%d failed: %s)",
                            retry_wait_s, source, attempt, max_attempts, str(e)[:80])
                await asyncio.sleep(retry_wait_s)
    return None


# ── METAR ─────────────────────────────────────────────────────────────────────

async def _do_fetch_metars(client: httpx.AsyncClient, conn: sqlite3.Connection) -> int:
    all_ids = ",".join(STATIONS.keys())
    r = await client.get(f"{AVWX_BASE}/metar",
                         params={"ids": all_ids, "format": "json", "hours": 2},
                         timeout=20)
    r.raise_for_status()
    data = r.json()

    # Dedupe — keep newest per station
    seen: dict[str, dict] = {}
    for obs in data:
        sid = obs.get("stationId") or obs.get("icaoId", "")
        if sid and sid not in seen:
            seen[sid] = obs

    ts = _now()
    saved = 0
    for sid, info in STATIONS.items():
        obs = seen.get(sid)
        if not obs or obs.get("temp") is None:
            continue
        temp_c = obs["temp"]
        conn.execute("""
            INSERT INTO wx_observations (station, city, ts_utc, temp_c, daily_high_c, source)
            VALUES (?,?,?,?,NULL,'metar')
        """, (sid, info["city"], ts, temp_c))
        conn.commit()

        running_high = _running_daily_high(conn, sid)
        conn.execute("""
            UPDATE wx_observations SET daily_high_c=?
            WHERE station=? AND ts_utc=? AND source='metar'
        """, (running_high, sid, ts))

        # HK anomaly
        if sid == HK_STATION:
            conn.execute("""
                INSERT INTO wx_observations (station, city, ts_utc, temp_c, daily_high_c, source)
                VALUES (?,?,?,?,?,'metar')
            """, (sid, "Hong Kong", ts, temp_c, running_high))

        obs_time = str(obs.get("reportTime", obs.get("obsTime", "")))[:16]
        log.info("  %-6s %-12s  temp=%5.1f°C  day_high=%s  metar=%s",
                 sid, info["city"], temp_c,
                 f"{running_high:.1f}°C" if running_high else "  n/a",
                 obs_time)
        saved += 1

    conn.commit()
    return saved


async def fetch_metars(conn: sqlite3.Connection) -> None:
    async with httpx.AsyncClient() as client:
        await with_retry(
            lambda: _do_fetch_metars(client, conn),
            conn, source="metar", station="batch"
        )


# ── TAF ───────────────────────────────────────────────────────────────────────

def _parse_taf_time(code: str, base_date: dt.date) -> str | None:
    """Convert TAF time code like '0106Z' → ISO UTC string."""
    try:
        day = int(code[:2])
        hh  = int(code[2:4])
        # TAF day codes can roll into next month
        month = base_date.month
        year  = base_date.year
        if day < base_date.day:
            month = (month % 12) + 1
            if month == 1:
                year += 1
        return dt.datetime(year, month, day, hh, 0, tzinfo=dt.timezone.utc).isoformat()
    except Exception:
        return None


async def _do_fetch_tafs(client: httpx.AsyncClient, conn: sqlite3.Connection) -> int:
    all_ids = ",".join(STATIONS.keys())
    r = await client.get(f"{AVWX_BASE}/taf",
                         params={"ids": all_ids, "format": "json", "hours": 36},
                         timeout=20)
    r.raise_for_status()
    data = r.json()

    ts     = _now()
    today  = dt.date.today()
    saved  = 0

    for taf in data:
        sid      = taf.get("icaoId", "")
        raw      = taf.get("rawTAF", "")
        issued   = taf.get("issueTime", "")
        vf_epoch = taf.get("validTimeFrom")
        vt_epoch = taf.get("validTimeTo")
        city     = STATIONS.get(sid, {}).get("city", sid)

        if not sid or not issued:
            continue

        vf = dt.datetime.fromtimestamp(vf_epoch, tz=dt.timezone.utc).isoformat() if vf_epoch else ""
        vt = dt.datetime.fromtimestamp(vt_epoch, tz=dt.timezone.utc).isoformat() if vt_epoch else ""

        txs = re.findall(r"TX(-?\d+)/(\d{4}Z)", raw)
        tns = re.findall(r"TN(-?\d+)/(\d{4}Z)", raw)

        tx_c    = max(int(t[0]) for t in txs) if txs else None
        tx_time = _parse_taf_time(
            max(txs, key=lambda t: int(t[0]))[1], today) if txs else None
        tn_c    = min(int(t[0]) for t in tns) if tns else None
        tn_time = _parse_taf_time(
            min(tns, key=lambda t: int(t[0]))[1], today) if tns else None

        try:
            conn.execute("""
                INSERT OR IGNORE INTO taf_forecasts
                    (station, city, issued_utc, valid_from_utc, valid_to_utc,
                     tx_c, tx_time_utc, tn_c, tn_time_utc, raw_taf, fetched_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (sid, city, issued, vf, vt, tx_c, tx_time, tn_c, tn_time, raw, ts))
            saved += 1
            if tx_c is not None:
                log.info("  %-6s %-12s  TAF TX=%d°C @%s  TN=%s°C",
                         sid, city, tx_c,
                         (tx_time or "")[:13],
                         str(tn_c) if tn_c is not None else "n/a")
        except Exception as e:
            log.warning("  TAF insert %s: %s", sid, e)

    conn.commit()
    return saved


async def fetch_tafs(conn: sqlite3.Connection) -> None:
    async with httpx.AsyncClient() as client:
        await with_retry(
            lambda: _do_fetch_tafs(client, conn),
            conn, source="taf", station="batch"
        )


# ── NWP Model forecasts ────────────────────────────────────────────────────────

def _model_run_utc(model: str) -> str:
    """Estimate the model run time that's currently being served by open-meteo."""
    cfg   = MODELS[model]
    now   = dt.datetime.now(dt.timezone.utc)
    delay = dt.timedelta(hours=cfg["fetch_offset_h"])
    runs  = sorted(cfg["runs_utc"], reverse=True)
    for run_h in runs:
        run_dt = now.replace(hour=run_h, minute=0, second=0, microsecond=0)
        if run_dt > now:
            run_dt -= dt.timedelta(days=1)
        if now - run_dt >= delay:
            return run_dt.isoformat()
    # Fallback: most recent run
    run_dt = now.replace(hour=runs[0], minute=0, second=0, microsecond=0)
    return run_dt.isoformat()


async def _do_fetch_model(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    model: str,
) -> int:
    ts       = _now()
    run_utc  = _model_run_utc(model)
    today    = dt.date.today()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    saved    = 0

    for sid, info in STATIONS.items():
        r = await client.get(OPEN_METEO, params={
            "latitude":        info["lat"],
            "longitude":       info["lon"],
            "models":          model,
            "daily":           "temperature_2m_max,temperature_2m_min",
            "forecast_days":   3,
            "temperature_unit":"celsius",
        }, timeout=15)
        r.raise_for_status()
        data  = r.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows  = daily.get("temperature_2m_min", [])

        for i, d in enumerate(dates):
            high = highs[i] if i < len(highs) else None
            low  = lows[i]  if i < len(lows)  else None
            horizon = (dt.date.fromisoformat(d) - today).days * 24
            conn.execute("""
                INSERT INTO model_forecasts
                    (station, city, model, model_run_utc, fetched_utc,
                     forecast_date, horizon_hours, high_c, low_c, lat, lon)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (sid, info["city"], model, run_utc, ts,
                  d, horizon, high, low, info["lat"], info["lon"]))
            saved += 1

        await asyncio.sleep(0.1)   # be polite to open-meteo

    conn.commit()
    return saved


async def fetch_model(conn: sqlite3.Connection, model: str) -> None:
    async with httpx.AsyncClient() as client:
        await with_retry(
            lambda: _do_fetch_model(client, conn, model),
            conn, source=model, station="all_airports"
        )


async def fetch_all_models(conn: sqlite3.Connection) -> None:
    """Fetch all 5 NWP models sequentially (rate-limit friendly)."""
    for model in MODELS:
        await fetch_model(conn, model)


# ── Scheduled loop ─────────────────────────────────────────────────────────────

def _next_run_times() -> dict[str, dt.datetime]:
    """
    Return the next UTC datetime each source should be fetched.
    Called once at startup and after each fetch to reschedule.
    """
    now = dt.datetime.now(dt.timezone.utc)
    schedule = {}

    # METAR: every 30 min on the :00 and :30
    next_metar = now.replace(second=0, microsecond=0)
    next_metar += dt.timedelta(minutes=30 - (next_metar.minute % 30))
    schedule["metar"] = next_metar

    # TAF: at 00:30, 06:30, 12:30, 18:30 UTC
    taf_hours = [0, 6, 12, 18]
    next_taf  = None
    for h in sorted(taf_hours * 2):
        candidate = now.replace(hour=h, minute=30, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1 if candidate.hour >= max(taf_hours) else 0)
        if next_taf is None or candidate < next_taf:
            next_taf = candidate
    schedule["taf"] = next_taf or (now + dt.timedelta(hours=6))

    # NWP models: based on run times + fetch offset
    for model, cfg in MODELS.items():
        candidates = []
        for run_h in cfg["runs_utc"]:
            fetch_time = now.replace(hour=run_h, minute=0, second=0, microsecond=0)
            fetch_time += dt.timedelta(hours=cfg["fetch_offset_h"])
            if fetch_time <= now:
                fetch_time += dt.timedelta(days=1)
            candidates.append(fetch_time)
        schedule[model] = min(candidates)

    return schedule


async def loop() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    log.info("Starting weather collection loop")
    log.info("  Sources: METAR(30min) + TAF(4x/day) + 5 NWP models (model-cadence)")

    schedule = _next_run_times()
    for source, t in sorted(schedule.items(), key=lambda x: x[1]):
        log.info("  Next %-22s → %s UTC", source, t.strftime("%H:%M"))

    while True:
        now = dt.datetime.now(dt.timezone.utc)
        due = {src: t for src, t in schedule.items() if t <= now}

        for source in due:
            log.info("── Running %s ──────────────────────────────", source)
            if source == "metar":
                await fetch_metars(conn)
            elif source == "taf":
                await fetch_tafs(conn)
            elif source in MODELS:
                await fetch_model(conn, source)

            # Reschedule this source
            if source == "metar":
                schedule[source] = now + dt.timedelta(minutes=30)
            elif source == "taf":
                schedule[source] = now + dt.timedelta(hours=6)
            else:
                cfg = MODELS[source]
                schedule[source] = now + dt.timedelta(hours=min(cfg["runs_utc"][1:]
                                                                 or [24]))

            log.info("  Next %-22s → %s UTC",
                     source, schedule[source].strftime("%Y-%m-%d %H:%M"))

        await asyncio.sleep(60)   # check schedule every minute


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",      action="store_true", help="Run full scheduled loop")
    parser.add_argument("--metar",     action="store_true", help="One METAR pass")
    parser.add_argument("--taf",       action="store_true", help="One TAF pass")
    parser.add_argument("--forecasts", action="store_true", help="One pass of all 5 models")
    parser.add_argument("--model",     help="One pass of a specific model")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    if args.loop:
        asyncio.run(loop())
    elif args.metar:
        asyncio.run(fetch_metars(conn))
    elif args.taf:
        asyncio.run(fetch_tafs(conn))
    elif args.forecasts:
        asyncio.run(fetch_all_models(conn))
    elif args.model:
        if args.model not in MODELS:
            print(f"Unknown model. Choose from: {list(MODELS.keys())}")
        else:
            asyncio.run(fetch_model(conn, args.model))
    else:
        parser.print_help()

    conn.close()
