# Polymarket Weather Market Logger — Project Plan

## What This Is

Polymarket currently tracks **17 city markets** across **16 unique settlement stations**
(Hong Kong and Beijing both map to ZBAA).

Each day: "Will the highest temperature in [City] be exactly X°C?" markets
settle at the day's official high temperature from a specific airport weather station
named in each market's resolution rules.

This project collects three categories of data:
1. **METAR observations** — live airport instrument readings used as a fast intraday proxy
2. **Forecast data** — 6 independent sources predicting the day's high at each airport, 48h ahead
3. **Order book snapshots** — YES/NO prices on every temperature bucket, every 2 minutes

Goal: Find gaps between what forecasts say, what the thermometer reads, and what the market prices.

---

## Settlement Stations

| City        | Station | Airport                        | Timezone         | Notes                         |
|-------------|---------|--------------------------------|------------------|-------------------------------|
| Seoul       | RKSI    | Incheon International          | Asia/Seoul       |                               |
| Hong Kong   | ZBAA    | Beijing Capital International  | Asia/Shanghai    | ⚠️ Anomaly — resolves on Beijing station |
| London      | EGLC    | London City Airport            | Europe/London    |                               |
| Tokyo       | RJTT    | Haneda Airport                 | Asia/Tokyo       |                               |
| NYC         | KLGA    | LaGuardia Airport              | America/New_York |                               |
| Paris       | LFPB    | Le Bourget Airport             | Europe/Paris     |                               |
| Beijing     | ZBAA    | Beijing Capital International  | Asia/Shanghai    |                               |
| Miami       | KMIA    | Miami International Airport    | America/New_York |                               |
| Singapore   | WSSS    | Changi Airport                 | Asia/Singapore   |                               |
| Madrid      | LEMD    | Barajas Airport                | Europe/Madrid    |                               |
| Moscow      | EFHK    | Helsinki Vantaa Airport        | Europe/Helsinki  | ⚠️ Anomaly — resolves on Helsinki station |
| Munich      | EDDM    | Munich Airport                 | Europe/Berlin    |                               |
| Amsterdam   | EHAM    | Schiphol Airport               | Europe/Amsterdam |                               |
| Ankara      | LTAC    | Ankara Esenboga Airport        | Europe/Istanbul  |                               |
| Wellington  | NZWN    | Wellington Airport             | Pacific/Auckland |                               |
| Shenzhen    | ZGSZ    | Shenzhen Bao'an Airport        | Asia/Shanghai    |                               |
| Guangzhou   | ZGGG    | Guangzhou Baiyun Airport       | Asia/Shanghai    |                               |

All station metadata (city, station, lat, lon, timezone, WU path, anomaly flags) lives in
`data/city_stations.json` as the single source of truth. All scripts load from there.

---

## Settlement Model — Three Layers

Do not treat any single data source as the definitive settlement truth. Three distinct layers exist:

| Layer | Source | Role |
|-------|--------|------|
| **Fast proxy** | METAR (aviationweather.gov) | Real-time intraday signal; drives the trade clock |
| **Settlement proxy** | Source named in market rules (e.g. WU daily summary for that station) | Closest to how Polymarket resolves; varies per market |
| **Final state** | Polymarket / UMA resolution outcome | Ground truth for research; only available after settlement |

The exact resolution source is stored per market in `rules_source` and `resolution_source_url`.
Do not hard-code "Weather Underground" as universal — individual markets may specify NOAA, WU,
a local met service, or another station.

`settle_markets.py` separates proxy settlement (METAR daily high at close time) from final
settlement (Polymarket/UMA resolved outcome), storing both.

---

## Data Sources

### Ground Truth — METAR (Fast Proxy)
**Source**: `aviationweather.gov/api/data/metar`
**What**: Live airport instrument readings. Used as a fast intraday proxy — the same
instrument family Weather Underground draws from, but METAR is real-time while WU publishes
a final daily summary that may differ (rounding, QA, data cutoff time).
**Frequency**: Every 30 minutes (stations update ~hourly; polling at 30 min catches every reading)
**Coverage**: All 16 unique settlement stations in a single batch API call
**Key fields**:
- `temp` → `temp_c`: current 2m temperature
- `reportTime` → `observed_utc`: when the reading was taken at the station (stored separately from `fetched_utc`)
- Running `daily_high_c` computed by `MAX(temp_c)` grouped by `(station, local_date)` — using the
  station's local timezone, not UTC, to avoid midnight boundary errors for non-UTC cities

---

### Forecast Sources

Six independent sources predicting the daily high at each airport's exact lat/lon, 48h ahead.
Fetched at each source's actual update cadence.

#### 1. TAF (Terminal Aerodrome Forecast)
**Source**: `aviationweather.gov/api/data/taf`
**What**: Official aviation forecast issued by the national met agency for that exact airport.
Where TX/TN temperature fields are present, this is the highest-quality forecast available
because it comes from the same institution as the METAR.
**Frequency**: Every 6h (issued at 00, 06, 12, 18 UTC). Fetch 30 min after issuance.
**Coverage**: All 16 stations fetched; only 5 include TX/TN temperature (RKSI, ZBAA, ZGGG, ZGSZ, LEMD).
**Horizon**: 30 hours (TAF format limitation — not full 48h)
**Timestamps stored**: `issued_utc`, `valid_from_utc`, `valid_to_utc`, `fetched_utc`

#### 2. ECMWF IFS — Direct from ECMWF Open Data
**Source**: `data.ecmwf.int` via `ecmwf-opendata` Python library
**What**: European Centre for Medium-Range Weather Forecasts IFS model. Gold standard globally.
0.25° resolution (~28km grid — all airports within 14km of a grid point).
**Why direct, not via open-meteo**: open-meteo mirrors ECMWF but adds 1–3h ingestion delay
on top of ECMWF's natural ~4–5h post-run time, yielding 6–8h total. Direct gives ~4–5h.
**Frequency**: 2× per day (00z and 12z runs). Fetch ~5h after run time (05:00 and 17:00 UTC).
**Coverage**: Global — all 16 airports confirmed.
**Format**: GRIB2, parsed with `cfgrib`. Extract 2m temp (`2t`) at steps +24h and +48h,
then compute daily max across 3-hourly steps within each airport's local calendar day.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

#### 3. GFS (Global Forecast System)
**Source**: `api.open-meteo.com` — model `gfs_seamless`
**What**: NOAA's primary global model.
**Frequency**: 4× per day. Fetch ~4h after each 00/06/12/18 UTC run.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

#### 4. ICON (Icosahedral Nonhydrostatic)
**Source**: `api.open-meteo.com` — model `icon_seamless`
**What**: DWD (German Weather Service) global model. Strong over Europe, solid globally.
**Frequency**: 4× per day. Fetch ~3h after run.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

#### 5. Météo-France ARPEGE
**Source**: `api.open-meteo.com` — model `meteofrance_seamless`
**What**: French national model. Good for European and tropical regions.
**Frequency**: 4× per day. Fetch ~3.5h after run.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

#### 6. GEM (Global Environmental Multiscale)
**Source**: `api.open-meteo.com` — model `gem_seamless`
**What**: Environment and Climate Change Canada's model.
**Frequency**: 4× per day. Fetch ~3.5h after run.
**Timestamps stored**: `model_run_utc`, `fetched_utc`

---

## Three Analytical Questions — Data Requirements

### Q1: What were the 5 models predicting 48h out at market open?

**What "market open" means**: `discover_markets.py` records `first_seen_utc` the moment it
first finds a market. This is used as a proxy for market open. It is *not* necessarily
the actual Polymarket creation timestamp — only use `first_seen_utc` for this purpose,
and only upgrade to a different field name if a true creation timestamp is available from
Polymarket/Gamma metadata.

**The query**:
```sql
-- Most recent forecast from each model that was available when market was first seen
SELECT mf.model, mf.high_c, mf.low_c, mf.fetched_utc, mf.model_run_utc
FROM model_forecasts mf
JOIN weather_markets wm ON mf.station = wm.station
                        AND mf.forecast_date = wm.settlement_date
WHERE wm.condition_id = ?
  AND mf.fetched_utc <= wm.first_seen_utc
GROUP BY mf.model
HAVING mf.fetched_utc = MAX(mf.fetched_utc)
```

**Requires**:
- `weather_markets.first_seen_utc` — set by `discover_markets.py` on first discovery
- `weather_markets.settlement_date`
- `model_forecasts.fetched_utc` — the moment this system acquired the forecast

---

### Q2: What was the actual temperature resolution?

**What "resolution" means**: Two separate answers exist:
- **Proxy resolution**: running METAR daily high at the station as of close time (fast, available same day)
- **Final resolution**: the Polymarket/UMA outcome (authoritative, available after settlement confirms)

**The query**:
```sql
SELECT wm.condition_id, wm.city, wm.settlement_date,
       wm.settlement_temp_c_proxy,   -- METAR daily high at close time
       wm.settlement_temp_c_final,   -- Polymarket/UMA resolved outcome (if available)
       wm.settlement_source,
       wm.settled_at_utc,
       wm.bucket_type, wm.temp_c,
       CASE
         WHEN wm.bucket_type = 'exact'    AND wm.settlement_temp_c_proxy = wm.temp_c  THEN 'YES'
         WHEN wm.bucket_type = 'above_eq' AND wm.settlement_temp_c_proxy >= wm.temp_c THEN 'YES'
         WHEN wm.bucket_type = 'below_eq' AND wm.settlement_temp_c_proxy <= wm.temp_c THEN 'YES'
         ELSE 'NO'
       END as resolved_proxy
FROM weather_markets wm
WHERE wm.settlement_date = ?
ORDER BY wm.city, wm.temp_c
```

**Requires**:
- `weather_markets.settlement_temp_c_proxy` — METAR daily high written by `settle_markets.py`
- `weather_markets.settlement_temp_c_final` — Polymarket/UMA outcome, written when available
- `weather_markets.settlement_source` — which source was used
- `weather_markets.settled_at_utc`

---

### Q3: Order book prices at open and at standard intervals to close?

**Standard intervals**: T-48h (open), T-24h, T-12h, T-6h, T-3h, T-1h, T-30min, T-close

**The query**:
```sql
SELECT ob.snapshot_label, ob.ts_utc, ob.hours_to_close,
       wm.temp_c, wm.bucket_type,
       ob.yes_bid, ob.yes_ask, ob.yes_mid
FROM ob_snapshots ob
JOIN weather_markets wm ON ob.condition_id = wm.condition_id
WHERE wm.city = ? AND wm.settlement_date = ?
ORDER BY ob.ts_utc, wm.temp_c
```

**Important**: `snapshot_label` is nullable. Every 2-minute raw snapshot is stored regardless.
Labels are assigned when a snapshot falls within 5 minutes of a standard threshold.
Analysis can select nearest snapshots even if a label window was missed.

**Requires**:
- `ob_snapshots.snapshot_label` — nullable; assigned at capture time near standard thresholds
- `ob_snapshots.hours_to_close` — computed at capture time from `close_time_utc`
- `weather_markets.first_seen_utc` and `close_time_utc` — to compute label offsets

---

## Fetch Schedule

| Source       | Runs/day | Fetch times (UTC)           | Delay after run | Horizon |
|--------------|----------|-----------------------------|-----------------|---------|
| METAR        | 48       | Every :00 and :30           | Real-time       | n/a     |
| TAF          | 4        | 00:30, 06:30, 12:30, 18:30  | 30 min          | 30h     |
| ECMWF direct | 2        | ~05:00, ~17:00              | ~5h             | 48h+    |
| GFS          | 4        | ~04:00, 10:00, 16:00, 22:00 | ~4h             | 48h     |
| ICON         | 4        | ~03:00, 09:00, 15:00, 21:00 | ~3h             | 48h     |
| Météo-France | 4        | ~03:30, 09:30, 15:30, 21:30 | ~3.5h           | 48h     |
| GEM          | 4        | ~03:30, 09:30, 15:30, 21:30 | ~3.5h           | 48h     |

Scheduler computes next intended fetch time from model run times and offsets after each job
completes — not a naive interval — to prevent drift from actual model cadence.

---

## Retry Policy

Every fetch is wrapped in `with_retry()`:
- **3 attempts** maximum per fetch cycle
- **180 second wait** between attempts
- Every attempt logged to `fetch_log` with exact UTC timestamp, duration in ms,
  status (`success` / `retry` / `failed`), records saved, and error message

---

## Database Schema

Single schema owner: `scripts/init_db.py`. Every script calls this before touching the DB.
No script defines its own table layout independently.

```sql
-- Live airport instrument readings (fast intraday proxy — NOT final settlement truth)
CREATE TABLE wx_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    observed_utc    TEXT,                  -- when the reading was taken at the station
    fetched_utc     TEXT NOT NULL,         -- when this system retrieved it
    local_date      TEXT NOT NULL,         -- YYYY-MM-DD in station's local timezone
    temp_c          REAL,
    daily_high_c    REAL,                  -- MAX(temp_c) by (station, local_date)
    source          TEXT DEFAULT 'metar'
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_wx_obs_unique
    ON wx_observations(station, source, observed_utc);
CREATE INDEX IF NOT EXISTS ix_wx_station_date ON wx_observations(station, local_date);

-- TAF aviation forecasts (TX/TN where available; only 5 of 16 stations include temp)
CREATE TABLE taf_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    issued_utc      TEXT NOT NULL,         -- when the TAF was issued
    valid_from_utc  TEXT NOT NULL,
    valid_to_utc    TEXT NOT NULL,
    fetched_utc     TEXT NOT NULL,         -- when this system retrieved it
    tx_c            REAL,                  -- forecast daily max
    tx_time_utc     TEXT,                  -- when max is expected
    tn_c            REAL,                  -- forecast daily min
    tn_time_utc     TEXT,
    raw_taf         TEXT,
    UNIQUE(station, issued_utc)
);

-- NWP model forecasts (GFS, ECMWF direct, ICON, Météo-France, GEM)
CREATE TABLE model_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station         TEXT NOT NULL,
    city            TEXT NOT NULL,
    model           TEXT NOT NULL,         -- ecmwf_direct | gfs_seamless | icon_seamless | etc.
    model_run_utc   TEXT NOT NULL,         -- which model run this data came from
    fetched_utc     TEXT NOT NULL,         -- when this system acquired it
    forecast_date   TEXT NOT NULL,         -- YYYY-MM-DD in station's local timezone
    horizon_hours   INTEGER,               -- 24 or 48
    high_c          REAL,
    low_c           REAL,
    lat             REAL,
    lon             REAL,
    UNIQUE(station, model, model_run_utc, forecast_date)
);

-- Polymarket weather markets (refreshed daily by discover_markets.py)
CREATE TABLE weather_markets (
    condition_id            TEXT PRIMARY KEY,
    city                    TEXT NOT NULL,
    station                 TEXT NOT NULL,
    settlement_date         TEXT NOT NULL,   -- YYYY-MM-DD in station's local timezone
    bucket_type             TEXT NOT NULL,   -- 'exact' | 'above_eq' | 'below_eq'
    temp_c                  REAL,
    yes_token_id            TEXT,
    no_token_id             TEXT,
    question                TEXT,
    rules_text              TEXT,            -- raw resolution rules from Polymarket
    rules_source            TEXT,            -- named source in rules (e.g. 'Weather Underground')
    resolution_source_url   TEXT,            -- exact URL named in rules
    first_seen_utc          TEXT,            -- when discover_markets.py first found this market
    close_time_utc          TEXT,            -- settlement UTC time from Polymarket end_date
    settlement_temp_c_proxy REAL,            -- Q2: METAR daily high at close time (fast proxy)
    settlement_temp_c_final REAL,            -- Q2: Polymarket/UMA resolved outcome (final truth)
    settlement_source       TEXT,            -- which source was used for proxy settlement
    resolution_status       TEXT,            -- 'proxy_only' | 'confirmed' | 'disputed'
    settled_at_utc          TEXT,            -- when settle_markets.py ran for this market
    active                  INTEGER DEFAULT 1,
    created_at              TEXT DEFAULT (datetime('now'))
);

-- Orderbook snapshots every 2 minutes (all snapshots kept; label is nullable)
CREATE TABLE ob_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    yes_bid         REAL,
    yes_ask         REAL,
    no_bid          REAL,
    no_ask          REAL,
    yes_mid         REAL,
    hours_to_close  REAL,                   -- computed at capture time
    snapshot_label  TEXT                    -- nullable: 'open'|'T-24h'|'T-12h'|'T-6h'|
                                            --          'T-3h'|'T-1h'|'T-30m'|'close'
);
CREATE INDEX IF NOT EXISTS ix_ob_cid_ts ON ob_snapshots(condition_id, ts_utc);

-- Complete fetch audit trail
CREATE TABLE fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    source      TEXT NOT NULL,              -- metar | taf | ecmwf_direct | gfs_seamless | etc.
    station     TEXT,
    attempt     INTEGER NOT NULL,           -- 1, 2, or 3
    status      TEXT NOT NULL,              -- success | retry | failed
    n_records   INTEGER DEFAULT 0,
    error       TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fetchlog_ts     ON fetch_log(ts_utc);
CREATE INDEX IF NOT EXISTS ix_fetchlog_source ON fetch_log(source, ts_utc);

-- Flagged opportunities
CREATE TABLE alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    city        TEXT NOT NULL,
    alert_type  TEXT NOT NULL,              -- neg_risk_gap | obs_mismatch | forecast_divergence | convergence
    detail_json TEXT
);
```

---

## File Structure

```
polymarket-weather/
├── scripts/
│   ├── init_db.py              # Single schema owner — all scripts call this first
│   ├── discover_markets.py     # Scrape Polymarket daily; set first_seen_utc, close_time_utc,
│   │                           # rules_source, resolution_source_url per market
│   ├── log_orderbooks.py       # Poll CLOB every 2 min; compute hours_to_close;
│   │                           # assign snapshot_label near standard thresholds
│   ├── fetch_weather.py        # METAR + TAF + 5 NWP models; scheduled loop
│   ├── settle_markets.py       # Runs 30min after close_time_utc per city; writes
│   │                           # settlement_temp_c_proxy + settlement_temp_c_final
│   └── neg_risk_scanner.py     # "above X°C" vs bucket sum + obs mismatch alerts
├── data/
│   └── city_stations.json      # Single source of truth: city, station, lat, lon,
│                               # timezone, country, wu_path, anomaly flags
├── logs/
│   └── research/
├── weather.db
└── PLAN.md
```

---

## Phase Plan

### Phase 1 — Data collection (in progress)
- [x] 17 city markets / 16 unique settlement stations identified and mapped
- [x] METAR fetch working — all 16 stations, correct resolution-source instrument
- [x] TAF fetch working — TX/TN parsed for 5 stations
- [x] GFS, ICON, Météo-France, GEM via open-meteo — all confirmed
- [x] Retry logic + fetch_log with exact timestamps and duration
- [ ] Add `timezone`, `lat`, `lon` to `data/city_stations.json`; refactor all scripts to load from there
- [ ] Fix daily high to use `local_date` (station timezone), not `DATE(ts_utc)`
- [ ] Create `scripts/init_db.py` as single schema owner
- [ ] ECMWF direct via `ecmwf-opendata` (replace open-meteo ECMWF — 3h faster)
- [ ] `discover_markets.py` refined: set `first_seen_utc`, `close_time_utc`,
      `rules_source`, `resolution_source_url`; prefer Gamma/CLOB structured metadata
      over HTML scraping where available
- [ ] `log_orderbooks.py`: compute `hours_to_close`, assign nullable `snapshot_label`
- [ ] `settle_markets.py`: write `settlement_temp_c_proxy` (METAR) and
      `settlement_temp_c_final` (Polymarket/UMA) with `resolution_status`
- [ ] Full scheduled loop running continuously

### Phase 2 — Gap detection
- [ ] Neg-risk scanner: "above X°C" YES ≠ sum of constituent bucket YES prices
- [ ] Obs mismatch: METAR daily high already exceeds a bucket still priced >5¢
- [ ] Forecast divergence: models spread >3°C on same airport — flag and watch market
- [ ] End-of-day convergence: last 60 min, obvious NO entries

### Phase 3 — Analysis (after 2+ weeks of data)
- [ ] Which forecast model best predicts actual METAR settlement reading?
- [ ] Do HK (ZBAA) and Moscow (EFHK) anomalies create systematic mispricings?
- [ ] Does model disagreement predict market mispricing?
- [ ] How often do neg-risk gaps appear, and how long do they last?
- [ ] Does WU data lag vs real-time METAR create a tradeable window?

---

## Key Open Questions

1. Which settlement source does each market actually name in its rules? (Must scrape per-market)
2. How much does WU final daily summary differ from raw METAR daily high? (Need to measure)
3. Which cities have the most market liquidity and tradeable spreads?
4. Does ECMWF direct beat open-meteo ECMWF by enough to matter for daily high forecasts?
5. Are HK and Moscow anomaly markets priced on the correct station or the wrong one?
