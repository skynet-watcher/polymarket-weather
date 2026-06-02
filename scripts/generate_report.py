"""
generate_report.py — build a static HTML dashboard from weather.db.

Called by GitHub Actions after each collection run.
Outputs docs/index.html (served by GitHub Pages).

Usage:
    python scripts/generate_report.py [--out docs/index.html]
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent.parent
DB_PATH   = REPO_ROOT / "weather.db"

MODEL_COLORS = {
    "ecmwf_ifs025":        "#22c55e",
    "gfs_seamless":        "#3b82f6",
    "icon_seamless":       "#f59e0b",
    "gem_seamless":        "#a78bfa",
    "meteofrance_seamless":"#14b8a6",
    "TAF":                 "#f472b6",
}
MODEL_NAMES = {
    "ecmwf_ifs025":        "ECMWF",
    "gfs_seamless":        "GFS",
    "icon_seamless":       "ICON",
    "gem_seamless":        "GEM",
    "meteofrance_seamless":"MF",
}

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _parse_utc(s):
    if not s: return None
    try: return dt.datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(dt.timezone.utc)
    except: return None

def _htc(close_utc):
    c = _parse_utc(close_utc)
    if not c: return None
    return (_now() - c).total_seconds() / -3600

def _status(htc, settled):
    if settled: return "settled"
    if htc is None: return "unknown"
    if htc < 0: return "post_close"
    if htc < 3: return "alert"
    return "open"

def _status_icon(s):
    return {"settled":"✅","alert":"🟡","post_close":"🔵","open":"🟢"}.get(s,"⚪")

def _mtype(city):
    A = {"Seoul","Tokyo","Beijing","Shenzhen","Guangzhou","Singapore","Hong Kong"}
    C = {"NYC","Miami","Wellington"}
    return "A" if city in A else ("C" if city in C else "B")

def build(db_path=DB_PATH) -> str:
    if not Path(db_path).exists():
        return "<html><body><h2>No data yet — collection starting up</h2></body></html>"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # ── summary stats ─────────────────────────────────────────────────────────
    def q(sql): return conn.execute(sql).fetchone()[0]
    total_markets = q("SELECT COUNT(*) FROM weather_markets WHERE active=1")
    total_snaps   = q("SELECT COUNT(*) FROM ob_snapshots")
    total_obs     = q("SELECT COUNT(*) FROM wx_observations")
    total_fcst    = q("SELECT COUNT(DISTINCT model) FROM model_forecasts")
    last_snap     = q("SELECT MAX(ts_utc) FROM ob_snapshots") or "—"
    last_metar    = q("SELECT MAX(fetched_utc) FROM wx_observations") or "—"
    open_alerts   = q("SELECT COUNT(*) FROM alerts WHERE status='open'")

    # ── city cards ────────────────────────────────────────────────────────────
    rows = conn.execute("""
        SELECT city, settlement_date, bucket_unit, close_time_utc,
               COUNT(*) as n_buckets
        FROM weather_markets WHERE active=1
        GROUP BY city, settlement_date
        ORDER BY city, settlement_date
    """).fetchall()

    # Deduplicate: nearest settlement per city
    now = _now()
    by_city = {}
    for r in rows:
        city = r["city"]
        htc = _htc(r["close_time_utc"])
        if city not in by_city:
            by_city[city] = dict(r)
        else:
            existing_htc = _htc(by_city[city]["close_time_utc"])
            # prefer the one with htc closer to 0 from the future
            curr_dist = abs(htc or 999)
            ex_dist   = abs(existing_htc or 999)
            if curr_dist < ex_dist:
                by_city[city] = dict(r)

    cards_html = ""
    for city, r in sorted(by_city.items()):
        sdate = r["settlement_date"]
        htc   = _htc(r["close_time_utc"])

        # Top bucket
        top = conn.execute("""
            SELECT wm.lower_temp, wm.upper_temp, wm.bucket_type, ob.yes_mid
            FROM weather_markets wm
            LEFT JOIN ob_snapshots ob ON ob.condition_id=wm.condition_id
                AND ob.ts_utc=(SELECT MAX(ts_utc) FROM ob_snapshots WHERE condition_id=wm.condition_id)
            WHERE wm.city=? AND wm.settlement_date=? AND wm.active=1
              AND wm.bucket_type NOT IN ('above_eq','below_eq')
            ORDER BY ob.yes_mid DESC LIMIT 1
        """, (city, sdate)).fetchone()

        # Latest METAR
        metar_high = conn.execute("""
            SELECT MAX(temp_c) FROM wx_observations WHERE city=? AND local_date=?
        """, (city, sdate)).fetchone()[0]

        # Forecasts — exact settlement date first, fallback to most recent available
        station_row = conn.execute(
            "SELECT station FROM weather_markets WHERE city=? AND active=1 LIMIT 1", (city,)
        ).fetchone()
        station_id = station_row["station"] if station_row else None
        forecasts = []
        if station_id:
            forecasts = conn.execute("""
                SELECT model, high_c FROM model_forecasts
                WHERE station=? AND forecast_date=?
                GROUP BY model HAVING fetched_utc=MAX(fetched_utc)
            """, (station_id, sdate)).fetchall()
            if not forecasts:
                forecasts = conn.execute("""
                    SELECT model, high_c FROM model_forecasts
                    WHERE station=?
                      AND forecast_date=(SELECT MAX(forecast_date) FROM model_forecasts WHERE station=?)
                    GROUP BY model HAVING fetched_utc=MAX(fetched_utc)
                """, (station_id, station_id)).fetchall()
        highs = [f["high_c"] for f in forecasts if f["high_c"]]
        fcst_avg = round(sum(highs)/len(highs), 1) if highs else None

        # Settled?
        settled = conn.execute("""
            SELECT lower_temp, upper_temp, bucket_type FROM weather_markets
            WHERE city=? AND settlement_date=? AND proxy_outcome='YES' LIMIT 1
        """, (city, sdate)).fetchone()

        status = _status(htc, settled)
        icon   = _status_icon(status)
        mtype  = _mtype(city)
        mtype_color = {"A":"#22c55e","B":"#f59e0b","C":"#3b82f6"}[mtype]

        # Time to close
        if htc is None:
            htc_str = "Close unknown"
        elif htc < 0:
            htc_str = f"Closed {-htc:.1f}h ago"
        elif htc < 1:
            htc_str = f"⚠ Closes in {htc*60:.0f}m"
        else:
            htc_str = f"Closes in {htc:.1f}h"

        # Top bucket bar
        bar_html = ""
        if top and top["yes_mid"] and not settled:
            pct = int(top["yes_mid"] * 100)
            t   = int(top["lower_temp"]) if top["lower_temp"] else "?"
            bar_html = f"""
            <div style="margin:4px 0 2px;font-size:11px;color:#94a3b8;display:flex;justify-content:space-between">
              <span>{t}°{r['bucket_unit']} top bucket</span>
              <span style="font-weight:700;color:#f1f5f9">{pct}¢</span>
            </div>
            <div style="background:#0f172a;border-radius:4px;height:8px;overflow:hidden">
              <div style="width:{pct}%;height:100%;background:#22c55e;border-radius:4px"></div>
            </div>"""

        settled_html = ""
        if settled:
            t = int(settled["lower_temp"]) if settled["lower_temp"] else "?"
            settled_html = f'<div style="background:#052e16;color:#22c55e;border-radius:6px;padding:8px;font-weight:700;font-size:13px;margin:4px 0">✅ Settled: {t}°{r["bucket_unit"]}</div>'

        cards_html += f"""
        <a href="https://github.com/skynet-watcher/polymarket-weather/actions" class="card status-{status}"
           style="display:block;text-decoration:none;color:#f1f5f9;background:#1e293b;
                  border:1px solid {'#22c55e44' if status=='settled' else '#f59e0b55' if status=='alert' else '#334155'};
                  border-radius:10px;padding:14px;transition:border-color .2s">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <span style="font-size:15px;font-weight:700">{icon} {city}</span>
            <span style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;
                          background:{mtype_color}22;color:{mtype_color}">Type {mtype}</span>
          </div>
          <div style="font-size:11px;color:#94a3b8;margin-bottom:8px">{sdate}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
            <div style="background:#0f172a;border-radius:6px;padding:8px">
              <div style="font-size:10px;color:#64748b;text-transform:uppercase">METAR high</div>
              <div style="font-size:17px;font-weight:700;color:{'#22c55e' if metar_high else '#64748b'}">{f'{metar_high:.1f}°' if metar_high else '—'}</div>
            </div>
            <div style="background:#0f172a;border-radius:6px;padding:8px">
              <div style="font-size:10px;color:#64748b;text-transform:uppercase">Fcst avg</div>
              <div style="font-size:17px;font-weight:700;color:{'#3b82f6' if fcst_avg else '#64748b'}">{f'{fcst_avg}°' if fcst_avg else '—'}</div>
            </div>
          </div>
          {settled_html}{bar_html}
          <div style="font-size:11px;color:#64748b;margin-top:8px">{htc_str}</div>
        </a>"""

    # ── model row ─────────────────────────────────────────────────────────────
    model_row = ""
    models_in_db = [r[0] for r in conn.execute(
        "SELECT DISTINCT model FROM model_forecasts").fetchall()]
    all_models = ["ecmwf_ifs025","gfs_seamless","icon_seamless","meteofrance_seamless","gem_seamless"]
    for m in all_models:
        name  = MODEL_NAMES.get(m, m)
        color = MODEL_COLORS.get(m, "#64748b")
        last  = conn.execute("SELECT MAX(fetched_utc) FROM model_forecasts WHERE model=?", (m,)).fetchone()[0]
        ok    = m in models_in_db
        dot   = f'<span style="color:{color if ok else "#334155"}">●</span>'
        time  = last[11:16] + " UTC" if last else "—"
        model_row += f'<div style="display:flex;align-items:center;gap:6px;padding:6px 10px;background:#0f172a;border-radius:6px">{dot}<span style="font-weight:600;color:{color if ok else "#475569"}">{name}</span><span style="font-size:11px;color:#64748b;margin-left:auto">{time}</span></div>'

    # ── alerts ─────────────────────────────────────────────────────────────────
    alert_rows = conn.execute("""
        SELECT city, alert_type, opened_utc FROM alerts WHERE status='open'
        ORDER BY opened_utc DESC LIMIT 10
    """).fetchall()
    alerts_html = ""
    if alert_rows:
        for a in alert_rows:
            atype  = a["alert_type"].replace("_"," ")
            color  = "#ef4444" if "neg_risk" in a["alert_type"] else "#f59e0b"
            alerts_html += f'<div style="padding:8px 12px;border-left:3px solid {color};background:{color}11;border-radius:0 6px 6px 0;margin-bottom:6px"><span style="font-size:11px;font-weight:700;color:{color};text-transform:uppercase">{atype}</span> <span style="color:#94a3b8;font-size:12px">— {a["city"]} · {a["opened_utc"][11:16]} UTC</span></div>'
    else:
        alerts_html = '<div style="color:#64748b;font-size:13px">✓ No open alerts</div>'

    conn.close()

    generated = _now().strftime("%Y-%m-%d %H:%M UTC")
    last_snap_fmt  = last_snap[11:16] + " UTC" if last_snap and last_snap != "—" else "—"
    last_metar_fmt = last_metar[11:16] + " UTC" if last_metar and last_metar != "—" else "—"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>Polymarket Weather · {generated}</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0f172a; color:#f1f5f9; font-family:system-ui,sans-serif;
       font-size:14px; line-height:1.5; padding:20px; }}
h2 {{ font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:.06em;
     color:#64748b; margin-bottom:10px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; }}
.stat {{ background:#1e293b; border:1px solid #334155; border-radius:8px;
         padding:12px 16px; }}
.stat-val {{ font-size:24px; font-weight:700; }}
.stat-lbl {{ font-size:11px; color:#64748b; margin-top:2px; }}
</style>
</head>
<body>
<div style="max-width:1400px;margin:0 auto">

  <!-- HEADER -->
  <div style="display:flex;justify-content:space-between;align-items:center;
               margin-bottom:20px;flex-wrap:wrap;gap:10px">
    <div>
      <div style="font-size:20px;font-weight:700">🌡️ <span style="color:#f59e0b">Polymarket</span> Weather</div>
      <div style="font-size:12px;color:#64748b;margin-top:2px">
        Static snapshot · refreshes every 10 min · updated {generated}
        · <a href="https://github.com/skynet-watcher/polymarket-weather/actions"
             style="color:#3b82f6">live logs ↗</a>
      </div>
    </div>
    {'<div style="background:#431407;color:#f59e0b;padding:6px 14px;border-radius:6px;font-size:13px;font-weight:600">🔔 ' + str(open_alerts) + ' open alert' + ('s' if open_alerts != 1 else '') + '</div>' if open_alerts else '<div style="background:#052e16;color:#22c55e;padding:6px 14px;border-radius:6px;font-size:13px;font-weight:600">✓ No alerts</div>'}
  </div>

  <!-- STATS ROW -->
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
               gap:10px;margin-bottom:20px">
    <div class="stat"><div class="stat-val">{total_markets}</div><div class="stat-lbl">Active markets</div></div>
    <div class="stat"><div class="stat-val">{total_snaps:,}</div><div class="stat-lbl">OB snapshots</div></div>
    <div class="stat"><div class="stat-val">{total_obs:,}</div><div class="stat-lbl">METAR obs</div></div>
    <div class="stat"><div class="stat-val" style="color:{'#22c55e' if total_fcst >= 5 else '#f59e0b'}">{total_fcst}/5</div><div class="stat-lbl">Forecast models</div></div>
    <div class="stat"><div class="stat-val" style="font-size:15px">{last_snap_fmt}</div><div class="stat-lbl">Last OB snapshot</div></div>
    <div class="stat"><div class="stat-val" style="font-size:15px">{last_metar_fmt}</div><div class="stat-lbl">Last METAR</div></div>
  </div>

  <!-- CITY GRID -->
  <h2 style="margin-bottom:12px">Cities</h2>
  <div class="grid" style="margin-bottom:24px">
    {cards_html}
  </div>

  <!-- MODELS + ALERTS -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">
    <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px">
      <h2 style="margin-bottom:10px">Forecast Models</h2>
      <div style="display:flex;flex-direction:column;gap:6px">{model_row}</div>
    </div>
    <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px">
      <h2 style="margin-bottom:10px">Open Alerts</h2>
      {alerts_html}
    </div>
  </div>

  <div style="font-size:11px;color:#334155;text-align:center">
    Data collected via GitHub Actions ·
    <a href="https://github.com/skynet-watcher/polymarket-weather" style="color:#475569">skynet-watcher/polymarket-weather</a>
  </div>
</div>
</body>
</html>"""
    return html


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/index.html")
    args = parser.parse_args()

    out = Path(REPO_ROOT / args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = build()
    out.write_text(html, encoding="utf-8")
    print(f"Report written to {out} ({len(html):,} bytes)")
