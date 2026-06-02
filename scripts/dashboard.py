"""
dashboard.py — Polymarket Weather monitoring dashboard.

Read-only. Serves from weather.db.

Usage:
    python scripts/dashboard.py
    # → http://localhost:8000
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

REPO_ROOT = Path(__file__).parent.parent
DB_PATH   = REPO_ROOT / "weather.db"
TMPL_DIR  = REPO_ROOT / "templates"

app       = FastAPI(title="Polymarket Weather Dashboard")
templates = Jinja2Templates(directory=str(TMPL_DIR))


# ── DB helpers ────────────────────────────────────────────────────────────────

def db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return None


def _hours_to(ts: str | None) -> float | None:
    t = _parse_utc(ts)
    if not t:
        return None
    return (t - _now_utc()).total_seconds() / 3600


# ── Data queries ──────────────────────────────────────────────────────────────

CITY_TZ = {}   # populated lazily from DB


def _city_tz(conn, city: str) -> str:
    if city not in CITY_TZ:
        row = conn.execute(
            "SELECT station FROM weather_markets WHERE city=? LIMIT 1", (city,)
        ).fetchone()
        # Read timezone from city_stations.json
        import json as _json
        cfg_path = REPO_ROOT / "data" / "city_stations.json"
        try:
            data = _json.loads(cfg_path.read_text())
            for c in data.get("cities", []):
                if c["city"] == city:
                    CITY_TZ[city] = c.get("timezone", "UTC")
                    break
        except Exception:
            pass
        if city not in CITY_TZ:
            CITY_TZ[city] = "UTC"
    return CITY_TZ[city]


def get_overview(conn) -> list[dict]:
    """One entry per city (today's or tomorrow's active settlement date)."""
    rows = conn.execute("""
        SELECT city, settlement_date, bucket_unit, close_time_utc,
               COUNT(*) as n_buckets,
               MIN(lower_temp) as min_temp, MAX(upper_temp) as max_temp
        FROM weather_markets
        WHERE active=1
        GROUP BY city, settlement_date
        ORDER BY city, settlement_date
    """).fetchall()

    # Deduplicate: pick nearest future settlement per city
    by_city: dict[str, dict] = {}
    now_utc = _now_utc()
    for r in rows:
        city  = r["city"]
        close = _parse_utc(r["close_time_utc"])
        if city in by_city:
            existing_close = _parse_utc(by_city[city]["close_time_utc"])
            # prefer the date whose close is soonest after now
            if close and existing_close:
                if abs((close - now_utc).total_seconds()) > abs((existing_close - now_utc).total_seconds()):
                    continue
        by_city[city] = dict(r)

    results = []
    for city, r in by_city.items():
        htc  = _hours_to(r["close_time_utc"])
        sdate = r["settlement_date"]

        # Top bucket by latest YES price
        top = conn.execute("""
            SELECT wm.lower_temp, wm.upper_temp, wm.bucket_type, wm.bucket_unit,
                   ob.yes_mid, ob.yes_bid, ob.yes_ask, ob.hours_to_close
            FROM weather_markets wm
            LEFT JOIN ob_snapshots ob ON ob.condition_id = wm.condition_id
                AND ob.ts_utc = (
                    SELECT MAX(ts_utc) FROM ob_snapshots WHERE condition_id=wm.condition_id
                )
            WHERE wm.city=? AND wm.settlement_date=? AND wm.active=1
              AND wm.bucket_type NOT IN ('above_eq','below_eq')
            ORDER BY ob.yes_mid DESC NULLS LAST
            LIMIT 1
        """, (city, sdate)).fetchone()

        # Latest METAR
        metar = conn.execute("""
            SELECT MAX(temp_c) as daily_high, MAX(observed_utc) as last_obs
            FROM wx_observations
            WHERE city=? AND local_date=?
        """, (city, sdate)).fetchone()

        # Settlement outcome
        settled = conn.execute("""
            SELECT wm.lower_temp, wm.upper_temp, wm.bucket_type, wm.proxy_outcome
            FROM weather_markets wm
            WHERE wm.city=? AND wm.settlement_date=? AND wm.proxy_outcome='YES'
            LIMIT 1
        """, (city, sdate)).fetchone()

        # Latest forecast — exact date first, then fallback to most recent available
        fcst = conn.execute("""
            SELECT AVG(high_c) as avg_high, COUNT(DISTINCT model) as n_models
            FROM model_forecasts
            WHERE station=(SELECT station FROM weather_markets WHERE city=? LIMIT 1)
              AND forecast_date=?
        """, (city, sdate)).fetchone()
        if not fcst or not fcst["avg_high"]:
            fcst = conn.execute("""
                SELECT AVG(high_c) as avg_high, COUNT(DISTINCT model) as n_models
                FROM model_forecasts
                WHERE station=(SELECT station FROM weather_markets WHERE city=? LIMIT 1)
                  AND forecast_date=(
                      SELECT MAX(forecast_date) FROM model_forecasts
                      WHERE station=(SELECT station FROM weather_markets WHERE city=? LIMIT 1)
                  )
            """, (city, city)).fetchone()

        # Status
        if settled:
            status = "settled"
        elif htc is not None and htc < 0:
            status = "post_close"
        elif htc is not None and htc < 3:
            status = "alert"
        elif htc is not None:
            status = "open"
        else:
            status = "unknown"

        # Market type + peak timing
        tz    = _city_tz(conn, city)
        mtype = _market_type(city)

        # Load peak hour from config
        import json as _j
        cfg_path = Path(__file__).parent.parent / "data" / "city_stations.json"
        peak_local_hour = None
        peak_std_h      = None
        try:
            cfg = _j.loads(cfg_path.read_text())
            cm  = next((c for c in cfg["cities"] if c["city"] == city), {})
            peak_local_hour = cm.get("typical_peak_local_hour")
            peak_std_h      = cm.get("peak_std_h")
        except Exception:
            pass

        peak_info = {}
        if peak_local_hour and tz:
            now_local = _now_utc().astimezone(ZoneInfo(tz))
            cur_h = now_local.hour + now_local.minute / 60
            htp = peak_local_hour - cur_h
            peak_info = {
                "peak_local_hour": peak_local_hour,
                "peak_std_h":      peak_std_h,
                "hours_to_peak":   round(htp, 1),
                "peak_passed":     htp < -(peak_std_h or 1.5),
                "at_peak":         abs(htp) <= (peak_std_h or 1.5),
            }

        results.append({
            "city":          city,
            "settlement_date": sdate,
            "bucket_unit":   r["bucket_unit"],
            "close_time_utc": r["close_time_utc"],
            "hours_to_close": round(htc, 1) if htc is not None else None,
            "status":        status,
            "market_type":   mtype,
            "timezone":      tz,
            "peak_info":     peak_info,
            "top_bucket":    dict(top) if top else None,
            "metar_high":    metar["daily_high"] if metar else None,
            "fcst_avg":      round(fcst["avg_high"], 1) if fcst and fcst["avg_high"] else None,
            "settled_bucket": dict(settled) if settled else None,
        })

    return sorted(results, key=lambda x: x["city"])


def _market_type(city: str) -> str:
    A = {"Seoul","Tokyo","Beijing","Shenzhen","Guangzhou","Singapore","Hong Kong"}
    C = {"NYC","Miami","Wellington"}
    if city in A: return "A"
    if city in C: return "C"
    return "B"


def get_city_detail(conn, city: str) -> dict:
    """Full data for one city's detail view."""
    # Pick nearest active settlement date
    row = conn.execute("""
        SELECT settlement_date, close_time_utc, bucket_unit, station
        FROM weather_markets WHERE city=? AND active=1
        ORDER BY ABS(JULIANDAY(close_time_utc) - JULIANDAY('now'))
        LIMIT 1
    """, (city,)).fetchone()
    if not row:
        return {}

    sdate   = row["settlement_date"]
    close   = row["close_time_utc"]
    unit    = row["bucket_unit"]
    station = row["station"]
    htc     = _hours_to(close)

    # Load peak timing from city_stations.json
    import json as _json
    cfg_path = REPO_ROOT / "data" / "city_stations.json"
    city_meta: dict = {}
    try:
        cfg = _json.loads(cfg_path.read_text())
        city_meta = next((c for c in cfg["cities"] if c["city"] == city), {})
    except Exception:
        pass
    peak_local_hour = city_meta.get("typical_peak_local_hour")
    peak_std_h      = city_meta.get("peak_std_h")
    tz_name         = city_meta.get("timezone", "UTC")

    # Compute hours until / since expected peak today (in local time)
    peak_info: dict = {}
    if peak_local_hour is not None:
        now_local = _now_utc().astimezone(ZoneInfo(tz_name))
        cur_local_h = now_local.hour + now_local.minute / 60
        hours_to_peak = peak_local_hour - cur_local_h
        # Peak already passed if hours_to_peak < 0
        peak_info = {
            "peak_local_hour": peak_local_hour,
            "peak_std_h":      peak_std_h,
            "hours_to_peak":   round(hours_to_peak, 1),
            "peak_passed":     hours_to_peak < -(peak_std_h or 1.5),
            "at_peak":         abs(hours_to_peak) <= (peak_std_h or 1.5),
            "peak_local_str":  f"{peak_local_hour:02d}:00 local",
            "tz_name":         tz_name,
        }

    # All buckets with latest price
    buckets = conn.execute("""
        SELECT wm.condition_id, wm.bucket_type, wm.lower_temp, wm.upper_temp,
               wm.bucket_unit, wm.proxy_outcome,
               ob.yes_bid, ob.yes_ask, ob.yes_mid, ob.spread, ob.hours_to_close,
               ob.ts_utc as price_ts
        FROM weather_markets wm
        LEFT JOIN ob_snapshots ob ON ob.condition_id = wm.condition_id
            AND ob.ts_utc = (
                SELECT MAX(ts_utc) FROM ob_snapshots WHERE condition_id=wm.condition_id
            )
        WHERE wm.city=? AND wm.settlement_date=? AND wm.active=1
        ORDER BY COALESCE(wm.lower_temp, wm.upper_temp) DESC
    """, (city, sdate)).fetchall()

    # All forecast runs for this station+date.
    # Primary: exact settlement_date match (live forecast).
    # Fallback: most recent available forecast_date for this station
    # (covers the gap before first live fetch, or if models skipped a day).
    all_forecasts = conn.execute("""
        SELECT model, high_c, low_c, fetched_utc, model_run_utc,
               forecast_date, horizon_hours, model_run_is_estimated
        FROM model_forecasts
        WHERE station=? AND forecast_date=?
        ORDER BY model, fetched_utc ASC
    """, (station, sdate)).fetchall()

    if not all_forecasts:
        # Fallback: pick the most recently fetched forecast for this station
        all_forecasts = conn.execute("""
            SELECT model, high_c, low_c, fetched_utc, model_run_utc,
                   forecast_date, horizon_hours, model_run_is_estimated
            FROM model_forecasts
            WHERE station=?
              AND forecast_date = (
                  SELECT MAX(forecast_date) FROM model_forecasts WHERE station=?
              )
            ORDER BY model, fetched_utc ASC
        """, (station, station)).fetchall()

    # Latest value per model (for summary)
    latest_fcst: dict[str, dict] = {}
    all_fcst_by_model: dict[str, list] = {}
    for f in all_forecasts:
        m = f["model"]
        latest_fcst[m] = dict(f)
        all_fcst_by_model.setdefault(m, []).append(dict(f))

    # Also pull TAF if available
    taf = conn.execute("""
        SELECT tx_c, tn_c, tx_time_utc, issued_utc, fetched_utc
        FROM taf_forecasts
        WHERE station=? ORDER BY issued_utc DESC LIMIT 1
    """, (station,)).fetchone()
    if taf and taf["tx_c"]:
        latest_fcst["TAF"] = dict(taf) | {"high_c": taf["tx_c"], "low_c": taf["tn_c"],
                                            "model": "TAF"}

    # Price history for chart (top 4 buckets by current price)
    top_cids = [b["condition_id"] for b in buckets
                if b["yes_mid"] is not None][:4]

    price_history = {}
    for cid in top_cids:
        ph = conn.execute("""
            SELECT ts_utc, yes_mid, hours_to_close
            FROM ob_snapshots
            WHERE condition_id=?
            ORDER BY ts_utc
        """, (cid,)).fetchall()
        bkt = next((b for b in buckets if b["condition_id"] == cid), None)
        if bkt:
            label = _bucket_label(dict(bkt))
            price_history[label] = [
                {"t": r["ts_utc"], "p": r["yes_mid"], "htc": r["hours_to_close"]}
                for r in ph if r["yes_mid"] is not None
            ]

    # METAR intraday
    metar_path = conn.execute("""
        SELECT local_hour, MAX(temp_c) as max_temp, MAX(observed_utc) as obs_utc
        FROM wx_observations
        WHERE city=? AND local_date=?
        GROUP BY local_hour
        ORDER BY local_hour
    """, (city, sdate)).fetchall()

    # Running daily high from METAR
    metar_high = conn.execute("""
        SELECT MAX(temp_c) FROM wx_observations WHERE city=? AND local_date=?
    """, (city, sdate)).fetchone()[0]

    # Alerts for this city
    alerts = conn.execute("""
        SELECT alert_type, opened_utc, detail_json, status
        FROM alerts
        WHERE city=? AND status='open'
        ORDER BY opened_utc DESC LIMIT 10
    """, (city,)).fetchall()

    # Settlement observation
    settle_obs = conn.execute("""
        SELECT value, unit, source_name, source_type, fetched_utc
        FROM settlement_observations
        WHERE city=? AND local_date=?
        ORDER BY fetched_utc DESC LIMIT 1
    """, (city, sdate)).fetchone()

    return {
        "city":            city,
        "settlement_date": sdate,
        "close_time_utc":  close,
        "bucket_unit":     unit,
        "station":         station,
        "hours_to_close":  round(htc, 2) if htc is not None else None,
        "status":          ("settled" if any(b["proxy_outcome"] == "YES" for b in buckets)
                            else ("post_close" if htc is not None and htc < 0 else "open")),
        "market_type":     _market_type(city),
        "timezone":        _city_tz(conn, city),
        "buckets":         [dict(b) for b in buckets],
        "forecasts":       latest_fcst,
        "all_fcst_by_model": all_fcst_by_model,
        "price_history":   price_history,
        "metar_path":      [dict(r) for r in metar_path],
        "metar_high":      metar_high,
        "peak_info":       peak_info,
        "alerts":          [dict(a) for a in alerts],
        "settle_obs":      dict(settle_obs) if settle_obs else None,
    }


def _bucket_label(b: dict) -> str:
    u = b.get("bucket_unit", "C")
    if b["bucket_type"] == "above_eq":
        return f"≥{b['lower_temp']:.0f}{u}"
    if b["bucket_type"] == "below_eq":
        return f"≤{b['upper_temp']:.0f}{u}"
    if b["bucket_type"] == "range":
        return f"{b['lower_temp']:.0f}-{b['upper_temp']:.0f}{u}"
    return f"{b['lower_temp']:.0f}{u}"


def get_health(conn) -> dict:
    """Data health metrics."""
    # Last fetch per source
    fetch_log = conn.execute("""
        SELECT source, MAX(ts_utc) as last_ts,
               SUM(CASE WHEN status='success' THEN n_records ELSE 0 END) as records_today,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failures
        FROM fetch_log
        WHERE ts_utc >= datetime('now','-24 hours')
        GROUP BY source ORDER BY source
    """).fetchall()

    # Last METAR per station
    stations = conn.execute("""
        SELECT station, city, MAX(observed_utc) as last_obs, MAX(temp_c) as last_temp
        FROM wx_observations
        GROUP BY station, city
        ORDER BY city
    """).fetchall()

    # Open alerts
    alerts = conn.execute("""
        SELECT alert_type, city, opened_utc, last_seen_utc, detail_json
        FROM alerts WHERE status='open'
        ORDER BY opened_utc DESC
    """).fetchall()

    # Settlement integrity
    integrity = conn.execute("""
        SELECT city, settlement_date,
               SUM(proxy_outcome='YES') as yes_count,
               SUM(proxy_outcome='NO') as no_count,
               SUM(proxy_outcome IS NULL) as pending
        FROM weather_markets
        WHERE settlement_value_proxy IS NOT NULL
        GROUP BY city, settlement_date
        ORDER BY settlement_date DESC, city
    """).fetchall()

    # Summary counts
    total_markets = conn.execute("SELECT COUNT(*) FROM weather_markets WHERE active=1").fetchone()[0]
    total_snaps   = conn.execute("SELECT COUNT(*) FROM ob_snapshots").fetchone()[0]
    last_snap     = conn.execute("SELECT MAX(ts_utc) FROM ob_snapshots").fetchone()[0]
    open_alerts   = conn.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]

    return {
        "fetch_log":   [dict(r) for r in fetch_log],
        "stations":    [dict(r) for r in stations],
        "alerts":      [dict(r) for r in alerts],
        "integrity":   [dict(r) for r in integrity],
        "total_markets": total_markets,
        "total_snaps":   total_snaps,
        "last_snap":     last_snap,
        "open_alerts":   open_alerts,
        "generated_at":  _now_utc().strftime("%Y-%m-%d %H:%M UTC"),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    conn = db()
    data = get_overview(conn)
    conn.close()
    return templates.TemplateResponse("overview.html", {
        "request":      request,
        "cities":       data,
        "generated_at": _now_utc().strftime("%H:%M UTC"),
        "open_alerts":  sum(1 for c in data if c["status"] == "alert"),
    })


@app.get("/city/{city_name}", response_class=HTMLResponse)
async def city_detail(request: Request, city_name: str):
    conn = db()
    data = get_city_detail(conn, city_name)
    conn.close()
    if not data:
        return HTMLResponse("<h1>City not found</h1>", status_code=404)
    data["bucket_label_fn"] = _bucket_label
    return templates.TemplateResponse("city.html", {
        "request": request,
        "d":       data,
        "bucket_label": _bucket_label,
        "json":    json,
    })


@app.get("/health", response_class=HTMLResponse)
async def health(request: Request):
    conn = db()
    data = get_health(conn)
    conn.close()
    return templates.TemplateResponse("health.html", {
        "request": request,
        "h":       data,
    })


@app.get("/api/overview")
async def api_overview():
    conn = db()
    data = get_overview(conn)
    conn.close()
    return data


@app.get("/api/city/{city_name}")
async def api_city(city_name: str):
    conn = db()
    data = get_city_detail(conn, city_name)
    conn.close()
    return data


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host="0.0.0.0", port=8000,
                reload=True, app_dir=str(Path(__file__).parent))
